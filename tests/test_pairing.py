import json
from uuid import uuid4

import httpx
import pytest
from conftest import FakeApp

from bax_codex_light.bridge import Bridge
from bax_codex_light.pairing import redeem
from bax_codex_light.registry import Registration, Registry


@pytest.mark.asyncio
async def test_auto_project_uses_exact_thread_not_process_directory(tmp_path):
    project = tmp_path / "Сайт с пробелами"
    project.mkdir()
    fake = FakeApp(project)
    async with fake.running() as endpoint:
        bridge = Bridge(None, Registry(tmp_path / "private.json"), endpoint=endpoint)
        await bridge.bind("current")
        assert bridge.project == project
        assert bridge.status()["needs_pairing"]
        with pytest.raises(ValueError):
            await bridge.bind("another-thread")
        await bridge.close()
    assert all(c["method"] != "turn/start" for c in fake.calls)


@pytest.mark.asyncio
async def test_pair_returns_no_permanent_key_and_keeps_existing_registration(tmp_path, monkeypatch):
    registration = Registration.from_key(f"{uuid4()}:{uuid4()}:" + "x" * 40, "ws://localhost/agent")

    async def fake_redeem(code, api):
        return registration

    monkeypatch.setattr("bax_codex_light.pairing.redeem", fake_redeem)
    registry = Registry(tmp_path / "private.json")
    bridge = Bridge(tmp_path, registry)
    with pytest.raises(ValueError):
        await bridge.pair("ABCD-EFGH-JKLM", "https://example.com")
    bridge.thread_id = "current"
    monkeypatch.setattr(bridge, "start", fake_start)
    result = await bridge.pair("ABCD-EFGH-JKLM", "https://example.com")
    assert registration.secret not in json.dumps(result)
    assert registry.get(tmp_path) == registration
    with pytest.raises(ValueError):
        await bridge.pair("ABCD-EFGH-JKLM", "https://example.com")


async def fake_start():
    pass


@pytest.mark.asyncio
async def test_redeem_does_not_follow_redirect_or_expose_secret(monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        assert json.loads(request.content) == {"code": "ABCDEFGHJKLM"}
        return httpx.Response(302, headers={"Location": "https://other.invalid/key"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(ValueError, match="Код неверен"):
        await redeem("ABCD-EFGH-JKLM")
    with pytest.raises(ValueError, match="HTTPS"):
        await redeem("ABCD-EFGH-JKLM", "http://remote.invalid")
