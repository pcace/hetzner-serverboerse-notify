from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Iterable

import certifi
import requests

AUCTION_DATA_URL = "https://www.hetzner.com/_resources/app/data/app/live_data_sb_EUR.json"
AUCTION_PAGE_URL = "https://www.hetzner.com/sb/"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "hetzner-serverboerse-notify/2.0"
DISK_TYPE_SSD_NVME = "ssd_nvme"
DISK_TYPE_HDD = "hdd"
DISK_TYPE_MIXED = "mixed"
STORAGE_MEDIUM_HDD = "hdd"
STORAGE_MEDIUM_SATA = "sata"
STORAGE_MEDIUM_NVME = "nvme"


def normalize_disk_type(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None

    normalized = "".join(character for character in text.lower() if character.isalnum())
    if normalized in {"ssd", "satassd", "sata", "nvme", "ssdnvme", "nvmessd", "solidstate"}:
        return DISK_TYPE_SSD_NVME
    if normalized == DISK_TYPE_HDD:
        return DISK_TYPE_HDD
    if normalized == DISK_TYPE_MIXED:
        return DISK_TYPE_MIXED
    raise ValueError("disk type must be one of: ssd/nvme, sata, nvme, hdd, mixed")


def describe_disk_type(value: str | None) -> str:
    if value == DISK_TYPE_SSD_NVME:
        return "SSD/NVMe"
    if value == DISK_TYPE_HDD:
        return "HDD"
    if value == DISK_TYPE_MIXED:
        return "Mixed"
    return "Unknown"


def infer_disk_type(disks: tuple[str, ...]) -> str | None:
    has_hdd = False
    has_solid_state = False
    for disk in disks:
        disk_name = disk.lower()
        if "hdd" in disk_name:
            has_hdd = True
        if "ssd" in disk_name or "nvme" in disk_name:
            has_solid_state = True

    if has_hdd and has_solid_state:
        return DISK_TYPE_MIXED
    if has_hdd:
        return DISK_TYPE_HDD
    if has_solid_state:
        return DISK_TYPE_SSD_NVME
    return None


def extract_storage_media(payload: dict[str, Any], disks: tuple[str, ...]) -> tuple[str, ...]:
    server_disk_data = payload.get("serverDiskData") or {}
    storage_media = tuple(
        medium
        for medium in (STORAGE_MEDIUM_HDD, STORAGE_MEDIUM_SATA, STORAGE_MEDIUM_NVME)
        if server_disk_data.get(medium)
    )
    if storage_media:
        return storage_media

    has_hdd = False
    has_sata = False
    has_nvme = False
    for disk in disks:
        disk_name = disk.lower()
        if "hdd" in disk_name:
            has_hdd = True
        if "nvme" in disk_name:
            has_nvme = True
        elif "ssd" in disk_name:
            has_sata = True

    fallback_media: list[str] = []
    if has_hdd:
        fallback_media.append(STORAGE_MEDIUM_HDD)
    if has_sata:
        fallback_media.append(STORAGE_MEDIUM_SATA)
    if has_nvme:
        fallback_media.append(STORAGE_MEDIUM_NVME)
    return tuple(fallback_media)


@dataclass(slots=True, frozen=True)
class ServerOffer:
    id: int
    cpu: str
    ram_gb: int
    price_eur: float
    setup_price_eur: float
    disk_count: int
    disk_size_gb: int
    disks: tuple[str, ...]
    storage_media: tuple[str, ...]
    datacenter: str
    bandwidth_mbit: int
    specials: tuple[str, ...]
    fixed_price: bool
    next_reduce_seconds: int | None
    next_reduce_timestamp: int | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ServerOffer":
        disks = tuple(str(entry) for entry in payload.get("hdd_arr", []))
        return cls(
            id=int(payload["id"]),
            cpu=str(payload.get("cpu", "Unknown CPU")),
            ram_gb=int(payload.get("ram_size") or 0),
            price_eur=float(payload.get("price") or 0),
            setup_price_eur=float(payload.get("setup_price") or 0),
            disk_count=int(payload.get("hdd_count") or 0),
            disk_size_gb=int(payload.get("hdd_size") or 0),
            disks=disks,
            storage_media=extract_storage_media(payload, disks),
            datacenter=str(payload.get("datacenter", "")),
            bandwidth_mbit=int(payload.get("bandwidth") or 0),
            specials=tuple(str(entry) for entry in payload.get("specials", [])),
            fixed_price=bool(payload.get("fixed_price")),
            next_reduce_seconds=_optional_int(payload.get("next_reduce")),
            next_reduce_timestamp=_optional_int(payload.get("next_reduce_timestamp")),
        )

    @property
    def total_disk_gb(self) -> int:
        if self.disk_count and self.disk_size_gb:
            return self.disk_count * self.disk_size_gb
        return self.disk_size_gb

    @property
    def disk_type(self) -> str | None:
        has_hdd = STORAGE_MEDIUM_HDD in self.storage_media
        has_solid_state = STORAGE_MEDIUM_SATA in self.storage_media or STORAGE_MEDIUM_NVME in self.storage_media
        if has_hdd and has_solid_state:
            return DISK_TYPE_MIXED
        if has_hdd:
            return DISK_TYPE_HDD
        if has_solid_state:
            return DISK_TYPE_SSD_NVME
        return infer_disk_type(self.disks)

    @property
    def next_reduce_description(self) -> str:
        if self.fixed_price:
            return "Fixed price"
        if self.next_reduce_seconds is None:
            return "Unknown"
        if self.next_reduce_seconds < 60:
            return "< 1 min"
        hours, remainder = divmod(self.next_reduce_seconds, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def url(self) -> str:
        return AUCTION_PAGE_URL


@dataclass(slots=True, frozen=True)
class FilterCriteria:
    min_ram_gb: int | None = None
    max_price_eur: float | None = None
    min_disk_gb: int | None = None
    disk_type: str | None = None
    cpu_query: str | None = None
    datacenter_query: str | None = None

    def matches(self, offer: ServerOffer) -> bool:
        if self.min_ram_gb is not None and offer.ram_gb < self.min_ram_gb:
            return False
        if self.max_price_eur is not None and offer.price_eur > self.max_price_eur:
            return False
        if self.min_disk_gb is not None and offer.total_disk_gb < self.min_disk_gb:
            return False
        if self.disk_type is not None and offer.disk_type != self.disk_type:
            return False
        if self.cpu_query and self.cpu_query.lower() not in offer.cpu.lower():
            return False
        if self.datacenter_query and self.datacenter_query.lower() not in offer.datacenter.lower():
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_ram_gb": self.min_ram_gb,
            "max_price_eur": self.max_price_eur,
            "min_disk_gb": self.min_disk_gb,
            "disk_type": self.disk_type,
            "cpu_query": self.cpu_query,
            "datacenter_query": self.datacenter_query,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "FilterCriteria":
        payload = payload or {}
        return cls(
            min_ram_gb=_optional_int(payload.get("min_ram_gb")),
            max_price_eur=_optional_float(payload.get("max_price_eur")),
            min_disk_gb=_optional_int(payload.get("min_disk_gb")),
            disk_type=normalize_disk_type(payload.get("disk_type")),
            cpu_query=_optional_text(payload.get("cpu_query")),
            datacenter_query=_optional_text(payload.get("datacenter_query")),
        )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fetch_raw_payload(session: requests.Session | None = None) -> dict[str, Any]:
    owns_session = session is None
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    try:
        response = session.get(
            AUCTION_DATA_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            verify=certifi.where(),
        )
        response.raise_for_status()
        return response.json()
    finally:
        if owns_session:
            session.close()


def fetch_offers(session: requests.Session | None = None) -> list[ServerOffer]:
    payload = fetch_raw_payload(session=session)
    offers = [ServerOffer.from_payload(entry) for entry in payload.get("server", [])]
    return sorted(offers, key=lambda offer: (offer.price_eur, -offer.ram_gb, offer.id))


def filter_offers(offers: Iterable[ServerOffer], criteria: FilterCriteria) -> list[ServerOffer]:
    return [offer for offer in offers if criteria.matches(offer)]


def format_offer(offer: ServerOffer) -> str:
    disk_text = ", ".join(offer.disks) if offer.disks else "n/a"
    specials = ", ".join(offer.specials) if offer.specials else "none"
    return "\n".join(
        [
            f"#{offer.id} | {offer.cpu}",
            f"Price: {offer.price_eur:.2f} EUR/month | RAM: {offer.ram_gb} GB | Disk total: {offer.total_disk_gb} GB | Disk type: {describe_disk_type(offer.disk_type)}",
            f"Disks: {disk_text}",
            f"Datacenter: {offer.datacenter or 'n/a'} | Bandwidth: {offer.bandwidth_mbit} Mbit | Specials: {specials}",
            f"Next price change: {offer.next_reduce_description}",
            f"Auction page: {offer.url}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and filter the Hetzner server auction feed.")
    parser.add_argument("--min-ram", dest="min_ram_gb", type=int, help="Minimum RAM in GB")
    parser.add_argument("--max-price", dest="max_price_eur", type=float, help="Maximum monthly price in EUR")
    parser.add_argument("--min-disk", dest="min_disk_gb", type=int, help="Minimum total disk capacity in GB")
    parser.add_argument("--disk-type", dest="disk_type", help="Disk type filter: ssd/nvme, sata, nvme, hdd, or mixed")
    parser.add_argument("--cpu", dest="cpu_query", help="Case-insensitive CPU substring filter")
    parser.add_argument("--datacenter", dest="datacenter_query", help="Case-insensitive datacenter substring filter")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of offers to print")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    criteria = FilterCriteria(
        min_ram_gb=args.min_ram_gb,
        max_price_eur=args.max_price_eur,
        min_disk_gb=args.min_disk_gb,
        disk_type=normalize_disk_type(args.disk_type),
        cpu_query=_optional_text(args.cpu_query),
        datacenter_query=_optional_text(args.datacenter_query),
    )

    offers = fetch_offers()
    matches = filter_offers(offers, criteria)
    print(f"Fetched {len(offers)} offers, {len(matches)} matched.")
    for offer in matches[: args.limit]:
        print()
        print(format_offer(offer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())