from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .config import Settings


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    lowered = {key.lower(): value for key, value in mapping.items()}
    for name in names:
        if name in mapping:
            return mapping[name]
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    raw = dict(payload)
    nested = raw.get("obj")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key, value in raw.items():
            merged.setdefault(key, value)
        return merged
    return raw


@dataclass(slots=True)
class PlategaPayment:
    transaction_id: str
    status: str
    redirect_url: str
    payment_method: str
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class PlategaCallback:
    transaction_id: str
    status: str
    order_ref: str | None
    payment_method: str
    raw_payload: dict[str, Any]


class PlategaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.settings.platega_enabled:
            raise RuntimeError("Platega is not configured")
        assert self.settings.platega_merchant_id is not None
        assert self.settings.platega_secret is not None
        return {
            "Content-Type": "application/json",
            "X-MerchantId": self.settings.platega_merchant_id,
            "X-Secret": self.settings.platega_secret,
        }

    def verify_callback_headers(self, headers: Mapping[str, str]) -> bool:
        if not self.settings.platega_enabled:
            return False
        merchant = headers.get("X-MerchantId") or headers.get("x-merchantid")
        secret = headers.get("X-Secret") or headers.get("x-secret")
        return (
            merchant == self.settings.platega_merchant_id
            and secret == self.settings.platega_secret
        )

    async def create_payment(
        self,
        *,
        order_ref: str,
        amount_rub: int,
        description: str,
    ) -> PlategaPayment:
        endpoint = "/transaction/process"
        payload: dict[str, Any] = {
            "paymentDetails": {
                "amount": amount_rub,
                "currency": "RUB",
            },
            "description": description,
            "payload": order_ref,
        }
        if self.settings.platega_payment_method is not None:
            payload["paymentMethod"] = self.settings.platega_payment_method
        else:
            endpoint = "/v2/transaction/process"
        if self.settings.platega_return_url:
            payload["return"] = self.settings.platega_return_url
        if self.settings.platega_failed_url:
            payload["failedUrl"] = self.settings.platega_failed_url

        response = await self._client.post(
            self.settings.platega_base_url + endpoint,
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        raw = _normalize_payload(response.json())
        transaction_id = str(raw.get("transactionId") or "").strip()
        redirect_url = str(raw.get("redirect") or raw.get("url") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        payment_method = str(raw.get("paymentMethod") or "").strip()
        if not transaction_id or not redirect_url:
            raise RuntimeError("Platega did not return transaction details")
        return PlategaPayment(
            transaction_id=transaction_id,
            status=status,
            redirect_url=redirect_url,
            payment_method=payment_method,
            raw_payload=raw,
        )

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        response = await self._client.get(
            self.settings.platega_base_url + f"/transaction/{transaction_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        return _normalize_payload(response.json())

    def parse_callback(self, payload: Mapping[str, Any]) -> PlategaCallback:
        nested = _pick(payload, "data", "transaction", "body")
        source = nested if isinstance(nested, Mapping) else payload
        transaction_id = str(_pick(source, "transactionId", "transaction_id", "id") or "").strip()
        status = str(_pick(source, "status") or "").strip().upper()
        order_ref_raw = _pick(source, "payload", "orderId", "order_id", "reference")
        order_ref = str(order_ref_raw).strip() if order_ref_raw not in (None, "") else None
        payment_method = str(_pick(source, "paymentMethod", "payment_method") or "").strip()
        return PlategaCallback(
            transaction_id=transaction_id,
            status=status,
            order_ref=order_ref,
            payment_method=payment_method,
            raw_payload=dict(payload),
        )
