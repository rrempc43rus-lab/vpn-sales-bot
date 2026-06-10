from __future__ import annotations

import asyncio
import base64
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings


def gb_to_bytes(gb: int) -> int:
    if gb <= 0:
        return 0
    return gb * 1024 * 1024 * 1024


def rewrite_local_url(external_base_url: str, target_url: str) -> str:
    try:
        target = urlsplit(target_url)
        external = urlsplit(external_base_url)
    except Exception:  # noqa: BLE001
        return target_url
    if target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return target_url
    if not external.hostname:
        return target_url
    port = f":{target.port}" if target.port else ""
    auth = ""
    if target.username:
        auth = target.username
        if target.password:
            auth += f":{target.password}"
        auth += "@"
    new_netloc = f"{auth}{external.hostname}{port}"
    return urlunsplit((target.scheme, new_netloc, target.path, target.query, target.fragment))


def sanitize_profile_name(raw_fragment: str) -> str:
    if not raw_fragment:
        return raw_fragment
    decoded = unquote(raw_fragment)
    cleaned = re.split(r"-tg\d+-[0-9a-f]{8,}.*$", decoded, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = cleaned.strip(" -_\t")
    if not cleaned:
        cleaned = decoded
    return quote(cleaned, safe="")


def normalize_share_link(external_base_url: str, link: str) -> str:
    rewritten = rewrite_local_url(external_base_url, link)
    try:
        parsed = urlsplit(rewritten)
    except Exception:  # noqa: BLE001
        return rewritten
    fragment = sanitize_profile_name(parsed.fragment)
    if parsed.scheme != "vless":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))
    params = parse_qsl(parsed.query, keep_blank_values=True)
    normalized: list[tuple[str, str]] = []
    values: dict[str, list[str]] = {}
    for key, value in params:
        values.setdefault(key, []).append(value)

    # Keep Reality links as simple and predictable as possible for mobile clients.
    if "encryption" not in values:
        values["encryption"] = ["none"]
    if any(value == "reality" for value in values.get("security", [])):
        values["spx"] = ["/"]

    ordered_keys = []
    for key, _ in params:
        if key not in ordered_keys:
            ordered_keys.append(key)
    for key in values:
        if key not in ordered_keys:
            ordered_keys.append(key)
    for key in ordered_keys:
        for value in values.get(key, []):
            normalized.append((key, value))
    query = urlencode(normalized, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, fragment))


def normalize_subscription_payload(external_base_url: str, content: bytes) -> bytes:
    raw_text = content.decode("utf-8", errors="ignore").strip()
    if not raw_text:
        return content

    def normalize_lines(text: str) -> str:
        lines = text.splitlines()
        normalized: list[str] = []
        changed = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("vless://", "vmess://", "trojan://")):
                updated = normalize_share_link(external_base_url, stripped)
                normalized.append(updated)
                changed = changed or updated != stripped
            else:
                normalized.append(line)
        if not changed:
            return text
        return "\n".join(normalized)

    direct = normalize_lines(raw_text)
    if direct != raw_text:
        return direct.encode("utf-8")

    try:
        decoded = base64.b64decode(raw_text, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001
        return content
    normalized_decoded = normalize_lines(decoded)
    if normalized_decoded == decoded:
        return content
    return base64.b64encode(normalized_decoded.encode("utf-8"))


@dataclass(slots=True)
class XuiInbound:
    id: int
    label: str
    protocol: str
    port: int


@dataclass(slots=True)
class ProvisionedClient:
    email: str
    sub_id: str
    subscription_url: str | None
    links: list[str]


class XuiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(verify=False, timeout=20.0, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._csrf_token: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _needs_csrf(method: str) -> bool:
        return method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}

    async def _fetch_csrf_token(self) -> str:
        response = await self._client.get(
            self.settings.xui_base_url.rstrip("/") + "/csrf-token",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("obj")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Unable to get CSRF token from 3x-ui")
        self._csrf_token = token
        return token

    async def _ensure_csrf_token(self, force_refresh: bool = False) -> str:
        if force_refresh or not self._csrf_token:
            return await self._fetch_csrf_token()
        return self._csrf_token

    async def _send(self, method: str, path: str, *, headers: dict[str, str], **kwargs: Any) -> httpx.Response:
        return await self._client.request(
            method,
            self.settings.xui_base_url.rstrip("/") + path,
            headers=headers,
            **kwargs,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with self._lock:
            extra_headers = kwargs.pop("headers", {})
            if not self._logged_in and path not in {"/csrf-token", "/login"}:
                await self.login()

            headers = {"X-Requested-With": "XMLHttpRequest", **extra_headers}
            if self._needs_csrf(method) and "X-CSRF-Token" not in headers:
                headers["X-CSRF-Token"] = await self._ensure_csrf_token()

            response = await self._send(method, path, headers=headers, **kwargs)
            if response.status_code in {401, 403}:
                self._logged_in = False
                self._csrf_token = None
                await self.login()
                headers = {"X-Requested-With": "XMLHttpRequest", **extra_headers}
                if self._needs_csrf(method):
                    headers["X-CSRF-Token"] = await self._ensure_csrf_token(force_refresh=True)
                response = await self._send(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()

    async def login(self) -> None:
        token = await self._fetch_csrf_token()
        response = await self._client.post(
            self.settings.xui_base_url.rstrip("/") + "/login",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data={
                "username": self.settings.xui_username,
                "password": self.settings.xui_password,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(payload.get("msg", "Unable to login into 3x-ui"))
        self._logged_in = True
        self._csrf_token = None

    async def list_inbounds(self) -> list[XuiInbound]:
        payload = await self._request("GET", "/panel/api/inbounds/list/slim")
        result = []
        for item in payload.get("obj", []) or []:
            result.append(
                XuiInbound(
                    id=int(item["id"]),
                    label=str(item.get("remark") or item.get("tag") or f"Inbound {item['id']}"),
                    protocol=str(item.get("protocol") or ""),
                    port=int(item.get("port") or 0),
                )
            )
        return result

    async def get_default_settings(self) -> dict[str, Any]:
        payload = await self._request("POST", "/panel/setting/defaultSettings")
        obj = payload.get("obj")
        return obj if isinstance(obj, dict) else {}

    async def provision_client(
        self,
        *,
        telegram_id: int,
        order_code: str,
        duration_days: int,
        traffic_gb: int,
        inbound_ids: list[int],
    ) -> ProvisionedClient:
        if not inbound_ids:
            raise RuntimeError("No inbound is attached to the selected plan")

        sub_id = uuid.uuid4().hex[:16]
        email = f"tg{telegram_id}-{order_code.lower()}"
        expires_at = datetime.now(UTC) + timedelta(days=duration_days)
        payload = {
            "client": {
                "email": email,
                "subId": sub_id,
                "id": str(uuid.uuid4()),
                "password": uuid.uuid4().hex[:16],
                "auth": uuid.uuid4().hex[:16],
                "flow": "",
                "security": "auto",
                "totalGB": gb_to_bytes(traffic_gb),
                "expiryTime": int(expires_at.timestamp() * 1000),
                "reset": 0,
                "limitIp": 0,
                "tgId": int(telegram_id),
                "group": "",
                "comment": f"Order {order_code}",
                "enable": True,
            },
            "inboundIds": inbound_ids,
        }
        response = await self._request("POST", "/panel/api/clients/add", json=payload)
        if not response.get("success"):
            raise RuntimeError(response.get("msg", "3x-ui rejected the client creation request"))

        defaults = await self.get_default_settings()
        subscription_url: str | None = None
        if defaults.get("subEnable") and isinstance(defaults.get("subURI"), str) and defaults["subURI"]:
            subscription_url = rewrite_local_url(
                self.settings.public_base_url,
                f"{defaults['subURI']}{sub_id}",
            )

        links_response = await self._request(
            "GET",
            f"/panel/api/clients/subLinks/{quote(sub_id, safe='')}",
        )
        links = [
            normalize_share_link(self.settings.public_base_url, str(item))
            for item in links_response.get("obj", []) or []
            if isinstance(item, str)
        ]
        if subscription_url is None and links:
            subscription_url = links[0]
        return ProvisionedClient(
            email=email,
            sub_id=sub_id,
            subscription_url=subscription_url,
            links=links,
        )
