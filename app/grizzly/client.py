from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import httpx
from app.config import settings


class GrizzlyError(RuntimeError):
    pass


class GrizzlyTransportError(GrizzlyError):
    pass


def _decimal(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (ValueError, TypeError):
        return None


def _flag(iso: str | None) -> str:
    iso = (iso or "").strip().upper()
    if len(iso) != 2 or not iso.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(ch)) for ch in iso)


class GrizzlyClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.grizzly_api_key
        self.base = (base_url or settings.grizzly_base_url).rstrip("/")

    async def request(self, **params) -> Any:
        params = {"api_key": self.api_key, **params}
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "QuickOTP/1.0"}) as client:
                r = await client.get(self.base, params=params)
                r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code >= 500:
                raise GrizzlyTransportError(str(exc)) from exc
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GrizzlyTransportError(str(exc)) from exc
        body = r.text.strip()
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            activation_id = payload.get("activationId") or payload.get("activation_id") or payload.get("id")
            phone = payload.get("phoneNumber") or payload.get("phone_number")
            cost = payload.get("activationCost") or payload.get("activation_cost") or payload.get("cost")
            sms = payload.get("sms") if isinstance(payload.get("sms"), dict) else {}
            otp = sms.get("code") or payload.get("code") or payload.get("otp")
            status = str(payload.get("status") or payload.get("activationStatus") or "").lower()
            if activation_id and phone:
                out = {"raw": body, "status": "ok", "activation_id": str(activation_id), "phone_number": str(phone)}
                if cost is not None:
                    out["activation_cost"] = _decimal(cost)
                return out
            if otp:
                return {"raw": body, "status": "ok", "otp": str(otp)}
            if status:
                return {"raw": body, "status": status, "data": payload}
        if body.startswith("ACCESS_NUMBER:"):
            parts = body.split(":", 2)
            return {"raw": body, "status": "ok", "activation_id": parts[1], "phone_number": parts[2]}
        if body.startswith("ACCESS_NUMBER_V2:"):
            parts = body.split(":", 2)
            return {"raw": body, "status": "ok", "activation_id": parts[1], "phone_number": parts[2]}
        if body.startswith("STATUS_OK:"):
            return {"raw": body, "status": "ok", "otp": body.split(":", 1)[1]}
        if body in {"STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_CANCEL", "NO_ACTIVATION", "ACCESS_ACTIVATION"}:
            return {"raw": body, "status": body}
        if body.startswith("ACCESS_BALANCE:"):
            return {"raw": body, "status": "ok", "balance": body.split(":", 1)[1]}
        return {"raw": body, "status": "error", "error": body}

    async def get_number(self, service: str, country: str, max_price=None, min_price=None):
        p = {"action": "getNumberV2", "service": service, "country": country}
        if max_price is not None:
            p["maxPrice"] = str(max_price)
        if min_price is not None:
            p["minPrice"] = str(min_price)
        return await self.request(**p)

    async def get_status(self, activation_id: str):
        return await self.request(action="getStatusV2", id=activation_id)

    async def set_status(self, activation_id: str, status: int):
        return await self.request(action="setStatus", id=activation_id, status=str(status))

    async def balance(self):
        return await self.request(action="getBalance")

    async def active_activations(self):
        return await self.request(action="getActiveActivations")

    async def get_price_matrix(self, service: str = "wa") -> Any:
        """Return Grizzly's live country/service price+stock matrix.

        Grizzly documents current price endpoints as getPrices/getPricesV2/getPricesV3;
        we prefer V3 and gracefully fall back for older API accounts.
        """
        last = None
        for action in ("getPricesV3", "getPricesV2", "getPrices"):
            try:
                result = await self.request(action=action, service=service)
                if result.get("status") != "error":
                    return result
                last = result
            except Exception as exc:
                last = exc
        raise GrizzlyError(f"Could not load Grizzly prices: {last}")

    async def get_countries(self) -> list[dict[str, Any]]:
        result = await self.request(action="getCountries")
        payload = result.get("data") if isinstance(result, dict) else result
        rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for code, value in payload.items():
                if isinstance(value, dict):
                    row = dict(value)
                else:
                    row = {"name": value}
                row.setdefault("code", code)
                rows.append(row)
        elif isinstance(payload, list):
            rows = [x for x in payload if isinstance(x, dict)]
        return rows

    @staticmethod
    def parse_available_rows(matrix: Any, service: str = "wa", countries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Normalize the different Grizzly price response shapes into country rows."""
        countries = countries or []
        names: dict[str, dict[str, Any]] = {}
        for row in countries:
            code = str(row.get("code") or row.get("id") or row.get("country") or "").strip()
            if code:
                names[code] = row

        out: dict[str, dict[str, Any]] = {}

        def add(code: Any, node: Any, parent: dict[str, Any] | None = None):
            if not isinstance(node, dict):
                return
            code = str(code or node.get("country") or node.get("countryId") or node.get("country_id") or node.get("id") or "").strip()
            if not code:
                return
            svc = node.get(service)
            if isinstance(svc, dict):
                merged = {**node, **svc}
            else:
                merged = dict(node)
            cost = merged.get("cost", merged.get("price", merged.get("activationCost", merged.get("amount"))))
            count = merged.get("count", merged.get("available", merged.get("stock", merged.get("qty", merged.get("total")))))
            name = merged.get("name") or merged.get("countryName") or names.get(code, {}).get("name") or f"Country {code}"
            iso = merged.get("iso") or merged.get("isoCode") or merged.get("countryIso") or names.get(code, {}).get("iso") or names.get(code, {}).get("isoCode")
            out[code] = {
                "code": code,
                "name": str(name),
                "iso": str(iso or "").upper(),
                "flag": _flag(str(iso or "")),
                "service": service,
                "cost": _decimal(cost),
                "count": _int(count),
            }

        def walk(node: Any):
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, dict):
                        if str(key).isdigit():
                            add(key, value)
                        elif key in {"data", "result", "prices", "countries"}:
                            walk(value)
                        else:
                            svc = value.get(service)
                            if isinstance(svc, dict):
                                add(key, value)
                            walk(value)
                    elif isinstance(value, list):
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict):
                        add(item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id"), item)
                        walk(item.get("data"))
                        walk(item.get("result"))
                        walk(item.get("prices"))

        walk(matrix)
        return sorted(out.values(), key=lambda r: ((r["cost"] is None, r["cost"] or Decimal("999999")), -(r["count"] or 0), r["name"]))
