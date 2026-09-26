from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings


class GrizzlyError(RuntimeError):
    """Base Grizzly integration error."""


class GrizzlyTransportError(GrizzlyError):
    """The request outcome is unknown because of a transport failure."""

    unknown_result = True


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

    async def request(self, **params) -> dict[str, Any]:
        params = {"api_key": self.api_key, **params}

        try:
            async with httpx.AsyncClient(
                timeout=30,
                headers={"User-Agent": "QuickOTP/1.0"},
            ) as client:
                response = await client.get(self.base, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code >= 500:
                raise GrizzlyTransportError(str(exc)) from exc
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise GrizzlyTransportError(str(exc)) from exc

        body = response.text.strip()

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            # Activation response.
            activation_id = (
                payload.get("activationId")
                or payload.get("activation_id")
                or payload.get("id")
            )
            phone = payload.get("phoneNumber") or payload.get("phone_number")
            cost = (
                payload.get("activationCost")
                or payload.get("activation_cost")
                or payload.get("cost")
            )
            sms = payload.get("sms") if isinstance(payload.get("sms"), dict) else {}
            otp = sms.get("code") or payload.get("code") or payload.get("otp")
            status = str(
                payload.get("status")
                or payload.get("activationStatus")
                or ""
            ).lower()

            if activation_id and phone:
                result = {
                    "raw": body,
                    "status": "ok",
                    "activation_id": str(activation_id),
                    "phone_number": str(phone),
                }
                if cost is not None:
                    result["activation_cost"] = _decimal(cost)
                return result

            if otp:
                return {"raw": body, "status": "ok", "otp": str(otp)}

            # IMPORTANT: getPrices/getPricesV2/getPricesV3 returns a matrix
            # such as:
            # {"1":{"wa":{"count":386331,"cost":0.56,"retry":0}}}
            # This is a successful response, not an error.
            if self._looks_like_price_matrix(payload):
                return {"raw": body, "status": "ok", "data": payload}

            # Country-list endpoints may return a normal JSON object/list.
            action = str(params.get("action") or "")
            if action in {"getCountries", "getCountryList"}:
                return {"raw": body, "status": "ok", "data": payload}

            if status:
                return {"raw": body, "status": status, "data": payload}

            # Preserve a JSON response instead of throwing it away.
            return {"raw": body, "status": "ok", "data": payload}

        # Legacy text responses.
        if body.startswith("ACCESS_NUMBER:"):
            parts = body.split(":", 2)
            if len(parts) == 3:
                return {
                    "raw": body,
                    "status": "ok",
                    "activation_id": parts[1],
                    "phone_number": parts[2],
                }

        if body.startswith("ACCESS_NUMBER_V2:"):
            parts = body.split(":", 2)
            if len(parts) == 3:
                return {
                    "raw": body,
                    "status": "ok",
                    "activation_id": parts[1],
                    "phone_number": parts[2],
                }

        if body.startswith("STATUS_OK:"):
            return {"raw": body, "status": "ok", "otp": body.split(":", 1)[1]}

        if body.startswith("ACCESS_BALANCE:"):
            return {"raw": body, "status": "ok", "balance": body.split(":", 1)[1]}

        if body in {
            "STATUS_WAIT_CODE",
            "STATUS_WAIT_RETRY",
            "STATUS_CANCEL",
            "NO_ACTIVATION",
            "ACCESS_ACTIVATION",
        }:
            return {"raw": body, "status": body}

        return {"raw": body, "status": "error", "error": body}

    @staticmethod
    def _looks_like_price_matrix(payload: dict[str, Any]) -> bool:
        for value in payload.values():
            if not isinstance(value, dict):
                continue
            for service_node in value.values():
                if not isinstance(service_node, dict):
                    continue
                has_price = "cost" in service_node or "price" in service_node
                has_stock = any(
                    key in service_node
                    for key in ("count", "stock", "available", "qty", "total")
                )
                if has_price and has_stock:
                    return True
        return False

    async def get_number(
        self,
        service: str,
        country: str,
        max_price=None,
        min_price=None,
    ):
        params = {
            "action": "getNumberV2",
            "service": service,
            "country": country,
        }
        if max_price is not None:
            params["maxPrice"] = str(max_price)
        if min_price is not None:
            params["minPrice"] = str(min_price)
        return await self.request(**params)

    async def get_status(self, activation_id: str):
        return await self.request(action="getStatusV2", id=activation_id)

    async def set_status(self, activation_id: str, status: int):
        return await self.request(
            action="setStatus",
            id=activation_id,
            status=str(status),
        )

    async def balance(self):
        return await self.request(action="getBalance")

    async def active_activations(self):
        return await self.request(action="getActiveActivations")

    async def get_price_matrix(self, service: str = "wa") -> dict[str, Any]:
        last: Any = None
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
        """Return Grizzly country metadata when the account/API exposes it.

        Some Grizzly accounts do not expose a separate country-list action.
        In that case return an empty list; the price matrix can still be used
        because it contains the country IDs, stock and cost.
        """
        for action in ("getCountries", "getCountryList"):
            try:
                result = await self.request(action=action)
                if result.get("status") == "error":
                    continue

                payload = result.get("data")
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

                if rows:
                    return rows
            except Exception:
                continue

        return []

    @staticmethod
    def parse_available_rows(
        matrix: Any,
        service: str = "wa",
        countries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        countries = countries or []
        names: dict[str, dict[str, Any]] = {}

        for row in countries:
            code = str(
                row.get("code")
                or row.get("id")
                or row.get("country")
                or ""
            ).strip()
            if code:
                names[code] = row

        out: dict[str, dict[str, Any]] = {}

        def add(code: Any, node: Any):
            if not isinstance(node, dict):
                return

            code = str(
                code
                or node.get("country")
                or node.get("countryId")
                or node.get("country_id")
                or node.get("id")
                or ""
            ).strip()
            if not code:
                return

            svc = node.get(service)
            merged = {**node, **svc} if isinstance(svc, dict) else dict(node)

            cost = merged.get(
                "cost",
                merged.get(
                    "price",
                    merged.get("activationCost", merged.get("amount")),
                ),
            )
            count = merged.get(
                "count",
                merged.get(
                    "available",
                    merged.get(
                        "stock",
                        merged.get("qty", merged.get("total")),
                    ),
                ),
            )

            metadata = names.get(code, {})
            name = (
                merged.get("name")
                or merged.get("countryName")
                or metadata.get("name")
                or f"Country {code}"
            )
            iso = (
                merged.get("iso")
                or merged.get("isoCode")
                or merged.get("countryIso")
                or metadata.get("iso")
                or metadata.get("isoCode")
            )

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
                # If this is the wrapped request result, enter its data.
                if "data" in node and isinstance(node["data"], (dict, list)):
                    walk(node["data"])

                for key, value in node.items():
                    if key == "data":
                        continue

                    if isinstance(value, dict):
                        if str(key).isdigit():
                            add(key, value)
                        elif key in {"result", "prices", "countries"}:
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
                        add(
                            item.get("country")
                            or item.get("countryId")
                            or item.get("country_id")
                            or item.get("id"),
                            item,
                        )
                        walk(item)

        walk(matrix)

        return sorted(
            out.values(),
            key=lambda row: (
                row["cost"] is None,
                row["cost"] or Decimal("999999"),
                -(row["count"] or 0),
                row["name"],
            ),
        )
