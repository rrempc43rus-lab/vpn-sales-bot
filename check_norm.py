from __future__ import annotations
import asyncio, os, base64
from pathlib import Path
import httpx

def load_env_file(path: str) -> None:
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()

load_env_file('/opt/vpn-sales-bot/.env')
from app.config import load_settings
from app.xui import normalize_subscription_payload

async def main():
    settings = load_settings()
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        raw = (await client.get('https://127.0.0.1:2096/sub/e3c4b90c78584c2b')).content
    norm = normalize_subscription_payload(settings.public_base_url, raw)
    print(base64.b64decode(raw).decode('utf-8','ignore'))
    print('---NORM---')
    print(base64.b64decode(norm).decode('utf-8','ignore'))

asyncio.run(main())
