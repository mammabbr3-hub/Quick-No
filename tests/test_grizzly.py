from decimal import Decimal
import pytest
from app.grizzly.client import GrizzlyClient

class Resp:
    text = '{"activationId":"123","phoneNumber":"15550001111","activationCost":"0.73"}'
    def json(self): return {"activationId":"123","phoneNumber":"15550001111","activationCost":"0.73"}
    def raise_for_status(self): pass

class StatusResp:
    text = '{"sms":{"code":"4821"}}'
    def json(self): return {"sms":{"code":"4821"}}
    def raise_for_status(self): pass

class FakeClient:
    def __init__(self, response): self.response=response
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    async def get(self,*args,**kwargs): return self.response

@pytest.mark.asyncio
async def test_parse_json_number(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: FakeClient(Resp()))
    out=await GrizzlyClient('x').get_number('wa','1',max_price=Decimal('1.00'))
    assert out['activation_id']=='123' and out['phone_number']=='15550001111'
    assert out['activation_cost']==Decimal('0.73')

@pytest.mark.asyncio
async def test_parse_json_otp(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: FakeClient(StatusResp()))
    out=await GrizzlyClient('x').get_status('123')
    assert out['otp']=='4821'


@pytest.mark.asyncio
async def test_transport_failure_is_unknown(monkeypatch):
    import httpx
    async def boom(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")
    class BrokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        get = boom
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: BrokenClient())
    from app.grizzly.client import GrizzlyTransportError
    with pytest.raises(GrizzlyTransportError):
        await GrizzlyClient('x').get_number('wa','1')


@pytest.mark.asyncio
async def test_server_error_is_unknown(monkeypatch):
    import httpx
    request = httpx.Request('GET', 'https://example.invalid')
    response = httpx.Response(503, request=request)
    class BrokenResponse:
        text = 'SERVICE_UNAVAILABLE'
        def json(self): return {}
        def raise_for_status(self): raise httpx.HTTPStatusError('503', request=request, response=response)
    class BrokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def get(self,*args,**kwargs): return BrokenResponse()
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: BrokenClient())
    from app.grizzly.client import GrizzlyTransportError
    with pytest.raises(GrizzlyTransportError):
        await GrizzlyClient('x').get_number('wa','1')
