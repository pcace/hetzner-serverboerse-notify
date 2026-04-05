# hetzner-serverboerse-notify

The original repository no longer worked as-is: it relied on Python 2 syntax, an outdated Scrapy layout, and HTML selectors that no longer match the current Hetzner Server Auction page.

This version uses Hetzner's current live JSON feed and adds a Telegram bot with per-chat filters.

## What it does

- fetches current auction offers from Hetzner's live JSON feed
- filters offers by RAM, price, disk capacity, disk type, CPU substring, and datacenter substring
- sends Telegram notifications for new matching offers only
- stores chat preferences locally in a JSON state file

## Files

- `scraper.py`: reusable fetch and filter logic plus a small CLI for manual checks
- `telegram_bot.py`: Telegram bot with persistent per-chat filters and polling

## Requirements

Create a virtual environment if you want, then install:

```bash
pip install -r requirements.txt
```

## Environment

Set at least:

```bash
export TELEGRAM_BOT_TOKEN="123456:replace-me"
```

Optional settings:

```bash
export POLL_INTERVAL_SECONDS=300
export STATE_FILE="state/subscriptions.json"
```

## Manual check

You can test the feed and filters without Telegram:

```bash
python3 scraper.py --min-ram 32 --max-price 35 --disk-type nvme --limit 5
```

## Run the bot

```bash
python3 telegram_bot.py
```

## Run with Docker Compose

1. Create a local env file:

```bash
cp .env.example .env
```

2. Set your bot token in `.env`.

3. Start the bot:

```bash
docker compose up -d --build
```

4. Check logs if needed:

```bash
docker compose logs -f
```

5. Stop it again:

```bash
docker compose down
```

The Compose setup stores subscription state in the Docker volume `bot_state`.

Then talk to your bot in Telegram and use commands like:

```text
/start
/set_min_ram 64
/set_max_price 35
/set_disk_type mixed
/set_cpu ryzen
/check
```

Use `off` to clear a single filter, for example:

```text
/set_max_price off
```

## Notes

- the bot marks existing matches as seen when you subscribe or change filters, so notifications are for new matching offers instead of a backlog flood
- notification messages include a deep link to the exact auction offer using Hetzner's `#search=<auction id>` filter
- Hetzner's actual Robot order flow is a form `POST`, so there is no stable shareable public order URL to put into Telegram directly
- for a host bind mount instead of the named Docker volume, adjust `docker-compose.yml` accordingly
