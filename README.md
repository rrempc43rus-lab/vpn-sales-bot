# VPN Sales Bot

Telegram bot plus a small FastAPI admin panel for selling VPN subscriptions through `3x-ui`.

## What it does

- shows active plans in Telegram
- creates orders for users
- creates payment pages through `Platega` when it is configured
- receives `Platega` callbacks and automatically confirms payments
- creates VPN clients in `3x-ui` and delivers the subscription to the user
- keeps a small admin panel for manual review and fallback actions

## Environment

Copy `.env.example` to `.env` and fill in the values.

Required core values:

- `BOT_TOKEN`
- `ADMIN_PASSWORD`
- `APP_SECRET_KEY`
- `XUI_BASE_URL`
- `XUI_USERNAME`
- `XUI_PASSWORD`

Optional `Platega` values:

- `PLATEGA_MERCHANT_ID`
- `PLATEGA_SECRET`
- `PLATEGA_PAYMENT_METHOD`
- `PLATEGA_RETURN_URL`
- `PLATEGA_FAILED_URL`

If `PLATEGA_MERCHANT_ID` and `PLATEGA_SECRET` are set, the bot will create payment links automatically.
If they are empty, the old manual payment flow stays available.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8085
```

## Platega callback

Set the callback URL in the `Platega` merchant cabinet to:

```text
https://your-domain.example/api/payment-callback
```

The callback must be public, HTTPS, and accept JSON `POST` requests.

## Notes

- the app uses long polling for Telegram, so no Telegram webhook is required
- before first delivery, create at least one inbound in `3x-ui` and attach it to a plan
- duplicate `Platega` callbacks are handled idempotently through stored transaction data

## Backups

The repository includes a ready backup script and systemd timer:

- `deploy/backup/vpn-sales-backup.sh`
- `deploy/systemd/vpn-sales-backup.service`
- `deploy/systemd/vpn-sales-backup.timer`

The backup stores:

- a snapshot of `/opt/vpn-sales-bot` without `.venv` and cache folders
- a consistent SQLite copy of `data/app.db`
- nginx config for the domain
- `x-ui` database and `config.json`

Backups are written to `/opt/backups/vpn-sales-bot` and old archives older than `14` days are removed automatically.
