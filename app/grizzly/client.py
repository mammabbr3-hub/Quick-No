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



# Grizzly's own country IDs (these are NOT international dialing codes).
# Source: Grizzly SMS country-code table; IDs are kept exactly as Grizzly
# expects them for API calls.
GRIZZLY_COUNTRIES: dict[str, tuple[str, str]] = {
    "1": ("Ukraine", "UA"), "2": ("Kazakhstan", "KZ"), "3": ("China", "CN"),
    "4": ("Philippines", "PH"), "6": ("Indonesia", "ID"), "7": ("Malaysia", "MY"),
    "8": ("Kenya", "KE"), "9": ("Tanzania", "TZ"), "10": ("Vietnam", "VN"),
    "11": ("Kyrgyzstan", "KG"), "12": ("USA (virtual)", "US"), "13": ("Israel", "IL"),
    "14": ("Hong Kong", "HK"), "15": ("Poland", "PL"), "16": ("United Kingdom", "GB"),
    "17": ("Madagascar", "MG"), "18": ("DR Congo", "CD"), "19": ("Nigeria", "NG"),
    "20": ("Macao", "MO"), "21": ("Egypt", "EG"), "22": ("India", "IN"),
    "23": ("Ireland", "IE"), "24": ("Cambodia", "KH"), "25": ("Laos", "LA"),
    "26": ("Haiti", "HT"), "27": ("Ivory Coast", "CI"), "28": ("Gambia", "GM"),
    "29": ("Serbia", "RS"), "30": ("Yemen", "YE"), "31": ("South Africa", "ZA"),
    "32": ("Romania", "RO"), "33": ("Colombia", "CO"), "34": ("Estonia", "EE"),
    "35": ("Azerbaijan", "AZ"), "36": ("Canada", "CA"), "37": ("Morocco", "MA"),
    "38": ("Ghana", "GH"), "39": ("Argentina", "AR"), "40": ("Uzbekistan", "UZ"),
    "41": ("Cameroon", "CM"), "42": ("Chad", "TD"), "43": ("Germany", "DE"),
    "44": ("Lithuania", "LT"), "45": ("Croatia", "HR"), "46": ("Sweden", "SE"),
    "48": ("Netherlands", "NL"), "49": ("Latvia", "LV"), "50": ("Austria", "AT"),
    "52": ("Thailand", "TH"), "53": ("Saudi Arabia", "SA"), "54": ("Mexico", "MX"),
    "55": ("Taiwan", "TW"), "56": ("Spain", "ES"), "58": ("Algeria", "DZ"),
    "59": ("Slovenia", "SI"), "60": ("Bangladesh", "BD"), "61": ("Senegal", "SN"),
    "62": ("Turkey", "TR"), "63": ("Czech Republic", "CZ"), "64": ("Sri Lanka", "LK"),
    "65": ("Peru", "PE"), "66": ("Pakistan", "PK"), "67": ("New Zealand", "NZ"),
    "68": ("Guinea", "GN"), "69": ("Mali", "ML"), "71": ("Ethiopia", "ET"),
    "72": ("Mongolia", "MN"), "73": ("Brazil", "BR"), "74": ("Afghanistan", "AF"),
    "75": ("Uganda", "UG"), "76": ("Angola", "AO"), "77": ("Cyprus", "CY"),
    "78": ("France", "FR"), "79": ("Papua New Guinea", "PG"), "80": ("Mozambique", "MZ"),
    "81": ("Nepal", "NP"), "82": ("Belgium", "BE"), "83": ("Bulgaria", "BG"),
    "84": ("Hungary", "HU"), "85": ("Moldova", "MD"), "86": ("Italy", "IT"),
    "87": ("Paraguay", "PY"), "88": ("Honduras", "HN"), "89": ("Tunisia", "TN"),
    "90": ("Nicaragua", "NI"), "91": ("Timor-Leste", "TL"), "92": ("Bolivia", "BO"),
    "93": ("Costa Rica", "CR"), "94": ("Guatemala", "GT"), "95": ("United Arab Emirates", "AE"),
    "96": ("Zimbabwe", "ZW"), "99": ("Togo", "TG"), "100": ("Kuwait", "KW"),
    "101": ("El Salvador", "SV"), "102": ("Tonga", "TO"), "103": ("Jamaica", "JM"),
    "104": ("Trinidad and Tobago", "TT"), "105": ("Ecuador", "EC"), "106": ("Eswatini", "SZ"),
    "107": ("Oman", "OM"), "108": ("Bosnia and Herzegovina", "BA"), "109": ("Dominican Republic", "DO"),
    "113": ("Cuba", "CU"), "114": ("Mauritania", "MR"), "115": ("Sierra Leone", "SL"),
    "116": ("Jordan", "JO"), "117": ("Portugal", "PT"), "118": ("Barbados", "BB"),
    "119": ("Burundi", "BI"), "120": ("Benin", "BJ"), "121": ("Brunei Darussalam", "BN"),
    "122": ("Bahamas", "BS"), "123": ("Botswana", "BW"), "124": ("Belize", "BZ"),
    "125": ("Central African Republic", "CF"), "126": ("Dominica", "DM"), "127": ("Grenada", "GD"),
    "128": ("Georgia", "GE"), "129": ("Greece", "GR"), "130": ("Guinea-Bissau", "GW"),
    "131": ("Guyana", "GY"), "132": ("Iceland", "IS"), "133": ("Comoros", "KM"),
    "134": ("Saint Kitts and Nevis", "KN"), "135": ("Liberia", "LR"), "136": ("Lesotho", "LS"),
    "137": ("Malawi", "MW"), "138": ("Namibia", "NA"), "139": ("Niger", "NE"),
    "140": ("Rwanda", "RW"), "141": ("Slovakia", "SK"), "142": ("Suriname", "SR"),
    "143": ("Tajikistan", "TJ"), "144": ("Monaco", "MC"), "145": ("Bahrain", "BH"),
    "146": ("Reunion", "RE"), "147": ("Zambia", "ZM"), "148": ("Armenia", "AM"),
    "149": ("Somalia", "SO"), "150": ("Republic of the Congo", "CG"), "151": ("Chile", "CL"),
    "152": ("Burkina Faso", "BF"), "154": ("Gabon", "GA"), "155": ("Albania", "AL"),
    "156": ("Uruguay", "UY"), "157": ("Mauritius", "MU"), "158": ("Bhutan", "BT"),
    "159": ("Maldives", "MV"), "160": ("Guadeloupe", "GP"), "161": ("Turkmenistan", "TM"),
    "162": ("French Guiana", "GF"), "163": ("Finland", "FI"), "164": ("Saint Lucia", "LC"),
    "165": ("Luxembourg", "LU"), "166": ("Saint Vincent and the Grenadines", "VC"),
    "167": ("Equatorial Guinea", "GQ"), "168": ("Djibouti", "DJ"), "169": ("Antigua and Barbuda", "AG"),
    "170": ("Cayman Islands", "KY"), "171": ("Montenegro", "ME"), "172": ("Denmark", "DK"),
    "173": ("Switzerland", "CH"), "174": ("Norway", "NO"), "175": ("Australia", "AU"),
    "176": ("Eritrea", "ER"), "177": ("South Sudan", "SS"), "178": ("Sao Tome and Principe", "ST"),
    "179": ("Aruba", "AW"), "180": ("Montserrat", "MS"), "181": ("Anguilla", "AI"),
    "182": ("Japan", "JP"), "183": ("North Macedonia", "MK"), "184": ("Seychelles", "SC"),
    "185": ("New Caledonia", "NC"), "186": ("Cape Verde", "CV"), "187": ("USA", "US"),
    "188": ("Palestine", "PS"), "189": ("Fiji", "FJ"), "199": ("Malta", "MT"),
    "201": ("Gibraltar", "GI"), "203": ("Kosovo", "XK"), "204": ("Niue", "NU"),
    "1003": ("Bermuda", "BM"), "1007": ("Vanuatu", "VU"), "1008": ("Greenland", "GL"),
    "1011": ("Martinique", "MQ"), "1012": ("French Polynesia", "PF"),
    "1027": ("Tonga", "TO"), "1062": ("Andorra", "AD"), "10348": ("Liechtenstein", "LI"),
    "10349": ("Sint Maarten", "SX"), "10350": ("South Korea", "KR"), "10351": ("Singapore", "SG"),
    "10161": ("American Samoa", "AS"), "10227": ("Tonga", "TO"), "10231": ("Samoa", "WS"),
}

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
            grizzly_name, grizzly_iso = GRIZZLY_COUNTRIES.get(code, ("", ""))
            name = (
                merged.get("name")
                or merged.get("countryName")
                or metadata.get("name")
                or grizzly_name
                or f"Country {code}"
            )
            iso = (
                merged.get("iso")
                or merged.get("isoCode")
                or merged.get("countryIso")
                or metadata.get("iso")
                or metadata.get("isoCode")
                or grizzly_iso
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
