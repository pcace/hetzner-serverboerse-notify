from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import (
    FilterCriteria,
    ServerOffer,
    describe_disk_type,
    fetch_offers,
    filter_offers,
    format_offer,
    normalize_disk_type,
)

DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_STATE_FILE = Path("state/subscriptions.json")
MAX_NOTIFIED_IDS = 2000
TELEGRAM_TEXT_LIMIT = 3800

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatSubscription:
    enabled: bool = True
    filters: FilterCriteria = field(default_factory=FilterCriteria)
    notified_offer_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "filters": self.filters.to_dict(),
            "notified_offer_ids": self.notified_offer_ids,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ChatSubscription":
        payload = payload or {}
        notified_offer_ids = [int(offer_id) for offer_id in payload.get("notified_offer_ids", [])]
        return cls(
            enabled=bool(payload.get("enabled", True)),
            filters=FilterCriteria.from_dict(payload.get("filters")),
            notified_offer_ids=notified_offer_ids[-MAX_NOTIFIED_IDS:],
        )


class SubscriptionStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._subscriptions = self._load()

    def _load(self) -> dict[int, ChatSubscription]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            int(chat_id): ChatSubscription.from_dict(subscription)
            for chat_id, subscription in payload.get("chats", {}).items()
        }

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chats": {
                str(chat_id): subscription.to_dict()
                for chat_id, subscription in sorted(self._subscriptions.items())
            }
        }
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    @staticmethod
    def _clone(subscription: ChatSubscription) -> ChatSubscription:
        return ChatSubscription.from_dict(subscription.to_dict())

    async def get_or_create(self, chat_id: int) -> ChatSubscription:
        async with self._lock:
            subscription = self._subscriptions.setdefault(chat_id, ChatSubscription())
            return self._clone(subscription)

    async def update(self, chat_id: int, mutator: Callable[[ChatSubscription], None]) -> ChatSubscription:
        async with self._lock:
            subscription = self._subscriptions.setdefault(chat_id, ChatSubscription())
            mutator(subscription)
            subscription.notified_offer_ids = subscription.notified_offer_ids[-MAX_NOTIFIED_IDS:]
            self._save_unlocked()
            return self._clone(subscription)

    async def list_all(self) -> dict[int, ChatSubscription]:
        async with self._lock:
            return {chat_id: self._clone(subscription) for chat_id, subscription in self._subscriptions.items()}

    async def baseline(self, chat_id: int, offer_ids: list[int]) -> ChatSubscription:
        async with self._lock:
            subscription = self._subscriptions.setdefault(chat_id, ChatSubscription())
            subscription.notified_offer_ids = offer_ids[-MAX_NOTIFIED_IDS:]
            self._save_unlocked()
            return self._clone(subscription)

    async def mark_notified(self, chat_id: int, offer_ids: list[int]) -> ChatSubscription:
        async with self._lock:
            subscription = self._subscriptions.setdefault(chat_id, ChatSubscription())
            seen = set(subscription.notified_offer_ids)
            for offer_id in offer_ids:
                if offer_id not in seen:
                    subscription.notified_offer_ids.append(offer_id)
                    seen.add(offer_id)
            subscription.notified_offer_ids = subscription.notified_offer_ids[-MAX_NOTIFIED_IDS:]
            self._save_unlocked()
            return self._clone(subscription)

    async def prune_seen_ids(self, current_offer_ids: set[int]) -> None:
        async with self._lock:
            changed = False
            for subscription in self._subscriptions.values():
                pruned = [offer_id for offer_id in subscription.notified_offer_ids if offer_id in current_offer_ids]
                if pruned != subscription.notified_offer_ids:
                    subscription.notified_offer_ids = pruned[-MAX_NOTIFIED_IDS:]
                    changed = True
            if changed:
                self._save_unlocked()


def get_store(application: Application) -> SubscriptionStore:
    return application.bot_data["store"]


def get_poll_interval(application: Application) -> int:
    return application.bot_data["poll_interval_seconds"]


def parse_optional_int(value: str) -> int | None:
    if value.lower() in {"off", "none", "reset", "clear"}:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError("value must be >= 0")
    return parsed


def parse_optional_float(value: str) -> float | None:
    if value.lower() in {"off", "none", "reset", "clear"}:
        return None
    parsed = float(value.replace(",", "."))
    if parsed < 0:
        raise ValueError("value must be >= 0")
    return parsed


def parse_optional_text(arguments: list[str]) -> str | None:
    text = " ".join(arguments).strip()
    if not text or text.lower() in {"off", "none", "reset", "clear"}:
        return None
    return text


def parse_optional_disk_type(arguments: list[str]) -> str | None:
    text = parse_optional_text(arguments)
    if text is None:
        return None
    return normalize_disk_type(text)


def format_filters(subscription: ChatSubscription) -> str:
    filters = subscription.filters
    return "\n".join(
        [
            f"Notifications: {'on' if subscription.enabled else 'paused'}",
            f"Min RAM: {filters.min_ram_gb if filters.min_ram_gb is not None else '-'} GB",
            f"Max price: {filters.max_price_eur if filters.max_price_eur is not None else '-'} EUR/month",
            f"Min disk total: {filters.min_disk_gb if filters.min_disk_gb is not None else '-'} GB",
            f"Disk type: {describe_disk_type(filters.disk_type) if filters.disk_type is not None else '-'}",
            f"CPU contains: {filters.cpu_query or '-'}",
            f"Datacenter contains: {filters.datacenter_query or '-'}",
        ]
    )


def build_help_text(poll_interval_seconds: int) -> str:
    return "\n".join(
        [
            "Hetzner Server Auction bot",
            "",
            f"Polling interval: {poll_interval_seconds} seconds",
            "",
            "Commands:",
            "/start - subscribe this chat without backfilling old offers",
            "/help - show this help",
            "/filters - show current filters",
            "/set_min_ram 64 - require at least 64 GB RAM",
            "/set_max_price 35 - require at most 35 EUR/month",
            "/set_min_disk 1000 - require at least 1000 GB total disk",
            "/set_disk_type ssd/nvme - require SSD/NVMe, HDD, or mixed disks",
            "/set_cpu ryzen - case-insensitive CPU substring filter",
            "/set_datacenter fsn1 - case-insensitive datacenter filter",
            "/check - show current matches right now",
            "/pause - stop notifications",
            "/resume - resume notifications and reset baseline",
            "/reset - clear filters and reset baseline",
            "",
            "Use 'off' instead of a value to clear a single filter.",
        ]
    )


def split_messages(header: str, offers: list[ServerOffer]) -> list[str]:
    messages: list[str] = []
    current = header.strip()
    for offer in offers:
        block = format_offer(offer)
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) > TELEGRAM_TEXT_LIMIT and current:
            messages.append(current)
            current = block
            continue
        current = candidate
    if current:
        messages.append(current)
    return messages


async def send_offer_digest(chat_id: int, offers: list[ServerOffer], bot, header: str) -> None:
    for message in split_messages(header, offers):
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            disable_web_page_preview=True,
        )


async def compute_current_matches(subscription: ChatSubscription) -> list[ServerOffer]:
    offers = fetch_offers()
    return filter_offers(offers, subscription.filters)


async def reset_baseline_for_chat(application: Application, chat_id: int) -> ChatSubscription:
    store = get_store(application)
    subscription = await store.get_or_create(chat_id)
    matches = await compute_current_matches(subscription)
    return await store.baseline(chat_id, [offer.id for offer in matches])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    store = get_store(context.application)
    await store.get_or_create(chat.id)
    try:
        await reset_baseline_for_chat(context.application, chat.id)
        subscription = await store.get_or_create(chat.id)
        await update.message.reply_text(
            "Registered this chat for Hetzner auction notifications. Existing matches were marked as seen, so you only get new matches from now on. Use /check for the current list."
        )
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Start baseline refresh failed: %s", exc)
        subscription = await store.get_or_create(chat.id)
        await update.message.reply_text("Chat registered, but the initial baseline refresh failed. Notifications may include current matches once.")
        await update.message.reply_text(format_filters(subscription))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(build_help_text(get_poll_interval(context.application)))


async def filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    subscription = await get_store(context.application).get_or_create(chat.id)
    await update.message.reply_text(format_filters(subscription))


async def update_numeric_filter(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, parser, label: str) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /{update.message.text.split()[0][1:]} <value|off>")
        return
    try:
        value = parser(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid value.")
        return

    store = get_store(context.application)

    def mutator(subscription: ChatSubscription) -> None:
        filters = subscription.filters.to_dict()
        filters[field_name] = value
        subscription.filters = FilterCriteria.from_dict(filters)

    subscription = await store.update(chat.id, mutator)
    try:
        subscription = await reset_baseline_for_chat(context.application, chat.id)
        suffix = "disabled" if value is None else f"set to {value}"
        await update.message.reply_text(f"{label} {suffix}. Existing matches were marked as seen.")
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Could not refresh baseline after numeric filter change: %s", exc)
        await update.message.reply_text("Filter saved, but baseline refresh failed. Notifications may include current matches once.")
        await update.message.reply_text(format_filters(subscription))


async def update_text_filter(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, label: str) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /{update.message.text.split()[0][1:]} <value|off>")
        return

    value = parse_optional_text(context.args)
    store = get_store(context.application)

    def mutator(subscription: ChatSubscription) -> None:
        filters = subscription.filters.to_dict()
        filters[field_name] = value
        subscription.filters = FilterCriteria.from_dict(filters)

    subscription = await store.update(chat.id, mutator)
    try:
        subscription = await reset_baseline_for_chat(context.application, chat.id)
        suffix = "disabled" if value is None else f"set to {value}"
        await update.message.reply_text(f"{label} {suffix}. Existing matches were marked as seen.")
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Could not refresh baseline after text filter change: %s", exc)
        await update.message.reply_text("Filter saved, but baseline refresh failed. Notifications may include current matches once.")
        await update.message.reply_text(format_filters(subscription))


async def set_min_ram_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_filter(update, context, field_name="min_ram_gb", parser=parse_optional_int, label="Min RAM")


async def set_max_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_filter(update, context, field_name="max_price_eur", parser=parse_optional_float, label="Max price")


async def set_min_disk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_filter(update, context, field_name="min_disk_gb", parser=parse_optional_int, label="Min disk")


async def set_disk_type_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_disk_type <ssd/nvme|sata|nvme|hdd|mixed|off>")
        return

    try:
        value = parse_optional_disk_type(context.args)
    except ValueError:
        await update.message.reply_text("Invalid value. Use ssd/nvme, sata, nvme, hdd, mixed, or off.")
        return

    store = get_store(context.application)

    def mutator(subscription: ChatSubscription) -> None:
        filters = subscription.filters.to_dict()
        filters["disk_type"] = value
        subscription.filters = FilterCriteria.from_dict(filters)

    subscription = await store.update(chat.id, mutator)
    try:
        subscription = await reset_baseline_for_chat(context.application, chat.id)
        suffix = "disabled" if value is None else f"set to {describe_disk_type(value)}"
        await update.message.reply_text(f"Disk type {suffix}. Existing matches were marked as seen.")
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Could not refresh baseline after disk type filter change: %s", exc)
        await update.message.reply_text("Filter saved, but baseline refresh failed. Notifications may include current matches once.")
        await update.message.reply_text(format_filters(subscription))


async def set_cpu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_text_filter(update, context, field_name="cpu_query", label="CPU contains")


async def set_datacenter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_text_filter(update, context, field_name="datacenter_query", label="Datacenter contains")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    subscription = await get_store(context.application).get_or_create(chat.id)
    try:
        matches = await compute_current_matches(subscription)
    except requests.RequestException as exc:
        LOGGER.warning("Manual check failed: %s", exc)
        await update.message.reply_text(f"Check failed: {exc}")
        return

    if not matches:
        await update.message.reply_text("No current offers match your filters.")
        return

    await send_offer_digest(
        chat_id=chat.id,
        offers=matches[:10],
        bot=context.bot,
        header=f"Current matches: {len(matches)} total, showing up to 10.",
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    def mutator(subscription: ChatSubscription) -> None:
        subscription.enabled = False

    subscription = await get_store(context.application).update(chat.id, mutator)
    await update.message.reply_text("Notifications paused.")
    await update.message.reply_text(format_filters(subscription))


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    store = get_store(context.application)

    def mutator(subscription: ChatSubscription) -> None:
        subscription.enabled = True

    subscription = await store.update(chat.id, mutator)
    try:
        subscription = await reset_baseline_for_chat(context.application, chat.id)
        await update.message.reply_text("Notifications resumed. Existing matches were marked as seen.")
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Resume baseline refresh failed: %s", exc)
        await update.message.reply_text("Notifications resumed, but baseline refresh failed.")
        await update.message.reply_text(format_filters(subscription))


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    store = get_store(context.application)

    def mutator(subscription: ChatSubscription) -> None:
        subscription.enabled = True
        subscription.filters = FilterCriteria()

    subscription = await store.update(chat.id, mutator)
    try:
        subscription = await reset_baseline_for_chat(context.application, chat.id)
        await update.message.reply_text("Filters reset. Existing matches were marked as seen.")
        await update.message.reply_text(format_filters(subscription))
    except requests.RequestException as exc:
        LOGGER.warning("Reset baseline refresh failed: %s", exc)
        await update.message.reply_text("Filters reset, but baseline refresh failed.")
        await update.message.reply_text(format_filters(subscription))


async def notify_subscribers(application: Application) -> None:
    store = get_store(application)
    offers = fetch_offers()
    current_offer_ids = {offer.id for offer in offers}
    await store.prune_seen_ids(current_offer_ids)
    subscriptions = await store.list_all()
    for chat_id, subscription in subscriptions.items():
        if not subscription.enabled:
            continue
        matches = filter_offers(offers, subscription.filters)
        seen_ids = set(subscription.notified_offer_ids)
        new_matches = [offer for offer in matches if offer.id not in seen_ids]
        if not new_matches:
            continue
        header = f"{len(new_matches)} new matching offer(s) found."
        try:
            await send_offer_digest(chat_id=chat_id, offers=new_matches, bot=application.bot, header=header)
            await store.mark_notified(chat_id, [offer.id for offer in new_matches])
        except TelegramError as exc:
            LOGGER.warning("Could not deliver notification to chat %s: %s", chat_id, exc)


async def poller_loop(application: Application, stop_event: asyncio.Event) -> None:
    interval = get_poll_interval(application)
    while not stop_event.is_set():
        try:
            await notify_subscribers(application)
        except requests.RequestException as exc:
            LOGGER.warning("Polling failed: %s", exc)
        except Exception:
            LOGGER.exception("Unexpected polling failure")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "subscribe this chat"),
            BotCommand("filters", "show active filters"),
            BotCommand("set_min_ram", "set minimum RAM in GB"),
            BotCommand("set_max_price", "set maximum monthly price"),
            BotCommand("set_min_disk", "set minimum total disk in GB"),
            BotCommand("set_disk_type", "set disk type filter"),
            BotCommand("set_cpu", "set CPU substring filter"),
            BotCommand("set_datacenter", "set datacenter filter"),
            BotCommand("check", "show current matches"),
            BotCommand("pause", "pause notifications"),
            BotCommand("resume", "resume notifications"),
            BotCommand("reset", "clear filters"),
            BotCommand("help", "show help"),
        ]
    )
    stop_event = asyncio.Event()
    poller_task = asyncio.create_task(poller_loop(application, stop_event), name="hetzner-poller")
    application.bot_data["poller_stop_event"] = stop_event
    application.bot_data["poller_task"] = poller_task


async def post_stop(application: Application) -> None:
    stop_event = application.bot_data.get("poller_stop_event")
    if stop_event is not None:
        stop_event.set()
    poller_task = application.bot_data.get("poller_task")
    if poller_task is not None:
        poller_task.cancel()
        with suppress(asyncio.CancelledError):
            await poller_task


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    state_file = Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE))
    poll_interval_seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS))
    if poll_interval_seconds <= 0:
        raise RuntimeError("POLL_INTERVAL_SECONDS must be > 0")

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    application.bot_data["store"] = SubscriptionStore(state_file)
    application.bot_data["poll_interval_seconds"] = poll_interval_seconds

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("filters", filters_command))
    application.add_handler(CommandHandler("set_min_ram", set_min_ram_command))
    application.add_handler(CommandHandler("set_max_price", set_max_price_command))
    application.add_handler(CommandHandler("set_min_disk", set_min_disk_command))
    application.add_handler(CommandHandler("set_disk_type", set_disk_type_command))
    application.add_handler(CommandHandler("set_cpu", set_cpu_command))
    application.add_handler(CommandHandler("set_datacenter", set_datacenter_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("reset", reset_command))
    return application


def main() -> int:
    application = build_application()
    application.run_polling(drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())