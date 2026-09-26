from __future__ import annotations
import httpx
import json
from decimal import Decimal, InvalidOperation
from app.config import settings

class GrizzlyError(RuntimeError):
    """Base Grizzly integration error."""


class GrizzlyTransportError(GrizzlyError):
    """The request outcome is unknown due to transport failure."""
    unknown_result = True

class GrizzlyClient:
    def __init__(self, api_key: str|None=None):
        self.api_key = api_key or settings.grizzly_api_key
        self.base = settings.grizzly_base_url.rstrip('/')
    async def request(self, **params):
        params['api_key'] = self.api_key
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(self.base, params=params)
                r.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise GrizzlyTransportError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            # A 5xx response does not establish whether the upstream action ran.
            # Treat it as unknown; callers must not blindly refund/retry.
            if exc.response is not None and exc.response.status_code >= 500:
                raise GrizzlyTransportError(str(exc)) from exc
            raise
        body = r.text.strip()
        # Grizzly deployments may return either legacy text or JSON for V2 endpoints.
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            activation_id = payload.get('activationId') or payload.get('activation_id') or payload.get('id')
            phone = payload.get('phoneNumber') or payload.get('phone_number')
            cost = payload.get('activationCost') or payload.get('activation_cost') or payload.get('cost')
            sms = payload.get('sms') if isinstance(payload.get('sms'), dict) else {}
            otp = sms.get('code') or payload.get('code') or payload.get('otp')
            status = str(payload.get('status') or payload.get('activationStatus') or '').lower()
            if activation_id and phone:
                out={'raw':body,'status':'ok','activation_id':str(activation_id),'phone_number':str(phone)}
                if cost is not None:
                    try: out['activation_cost']=Decimal(str(cost))
                    except (InvalidOperation, ValueError): pass
                return out
            if otp:
                return {'raw':body,'status':'ok','otp':str(otp)}
            if status:
                return {'raw':body,'status':status}
        if body.startswith('ACCESS_NUMBER:'):
            parts=body.split(':',2); return {'raw':body,'status':'ok','activation_id':parts[1],'phone_number':parts[2]}
        if body.startswith('ACCESS_NUMBER_V2:'):
            parts=body.split(':',2); return {'raw':body,'status':'ok','activation_id':parts[1],'phone_number':parts[2]}
        if body.startswith('STATUS_OK:'): return {'raw':body,'status':'ok','otp':body.split(':',1)[1]}
        if body in {'STATUS_WAIT_CODE','STATUS_WAIT_RETRY','STATUS_CANCEL','NO_ACTIVATION','ACCESS_ACTIVATION'}: return {'raw':body,'status':body}
        if body.startswith('ACCESS_BALANCE:'): return {'raw':body,'status':'ok','balance':body.split(':',1)[1]}
        return {'raw':body,'status':'error','error':body}
    async def get_number(self, service: str, country: str, max_price=None, min_price=None):
        p={'action':'getNumberV2','service':service,'country':country}
        if max_price is not None:p['maxPrice']=str(max_price)
        if min_price is not None:p['minPrice']=str(min_price)
        return await self.request(**p)
    async def get_status(self, activation_id: str):
        return await self.request(action='getStatusV2', id=activation_id)
    async def set_status(self, activation_id: str, status: int):
        return await self.request(action='setStatus', id=activation_id, status=str(status))
    async def balance(self): return await self.request(action='getBalance')
    async def active_activations(self): return await self.request(action='getActiveActivations')
