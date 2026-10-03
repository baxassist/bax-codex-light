import asyncio
import json
import stat
from unittest.mock import AsyncMock

import pytest
from conftest import FakeApp, until
from test_integration import registration
from test_power import Process
from websockets.asyncio.server import serve

from bax_codex_light.bridge import Bridge
from bax_codex_light.preferences import Preferences
from bax_codex_light.registry import Registry


def test_default_enabled_and_choices_isolated_by_project_and_agent(tmp_path):
    path = tmp_path / "settings.json"
    preferences = Preferences(path)
    first, second = tmp_path / "first", tmp_path / "second"
    assert preferences.get(first, "agent") is True
    preferences.put(first, "agent", False)
    preferences.put(second, "agent", False)
    preferences.put(first, "other", True)
    restored = Preferences(path)
    assert restored.get(first, "agent") is False
    assert restored.get(first, "other") is True
    assert restored.get(second, "agent") is False
    assert restored.get(second, "other") is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "secret" not in path.read_text()


@pytest.mark.parametrize("value", ["false", 0, None, {}])
def test_preferences_reject_non_boolean_without_changing_file(tmp_path, value):
    path = tmp_path / "settings.json"
    preferences = Preferences(path)
    preferences.put(tmp_path, "agent", False)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="true или false"):
        preferences.put(tmp_path, "agent", value)
    assert path.read_bytes() == before


def test_preferences_reject_symlink_and_corrupt_data(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    path = tmp_path / "settings.json"
    path.symlink_to(target)
    preferences = Preferences(path)
    with pytest.raises(OSError):
        preferences.put(tmp_path, "agent", False)
    assert target.read_text() == "{}"
    path.unlink()
    path.write_text("[]")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Повреждён"):
        preferences.get(tmp_path, "agent")


async def test_phone_toggle_applies_immediately_and_survives_bridge_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("bax_codex_light.power.sys.platform", "darwin")
    first, second = Process(), Process()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    fake = FakeApp(tmp_path)
    frames, sockets = [], []
    reg = None

    async def relay(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        sockets.append(ws)
        async for raw in ws:
            frames.append(json.loads(raw))

    def reply(rid):
        return next((f for f in frames if f["type"] == "power.settings" and f.get("rid") == rid), None)

    async with fake.running() as endpoint, serve(relay, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, reg)
        original_registration = registry.path.read_bytes()
        bridge = Bridge(tmp_path, registry, "current", endpoint)
        try:
            await bridge.start()
            await until(lambda: any(f["type"] == "power.settings" for f in frames))
            settings = next(f for f in frames if f["type"] == "power.settings")
            assert settings["enabled"] and settings["active"] and settings["supported"]
            assert {"power.get", "power.set"} <= set(
                next(f["supports"] for f in frames if f["type"] == "caps")
            )
            before_calls = list(fake.calls)
            await sockets[-1].send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "power.set",
                        "agent": reg.agent,
                        "keep_awake": False,
                        "rid": "off",
                        "permission_mode": "dontAsk",
                    }
                )
            )
            await until(lambda: reply("off"))
            assert not reply("off")["enabled"] and not reply("off")["active"]
            assert first.terminated == 1
            assert fake.calls == before_calls
            await bridge.close()

            bridge = Bridge(tmp_path, registry, "current", endpoint)
            await bridge.start()
            await until(lambda: len(sockets) == 2)
            await sockets[-1].send(json.dumps({"v": 1, "type": "power.get", "rid": "restored"}))
            await until(lambda: reply("restored"))
            assert not reply("restored")["enabled"] and not reply("restored")["active"]
            assert spawn.await_count == 1
            await sockets[-1].send(json.dumps({"v": 1, "type": "power.set", "keep_awake": True, "rid": "on"}))
            await until(lambda: reply("on"))
            assert reply("on")["enabled"] and reply("on")["active"]
            assert spawn.await_count == 2

            for rid, fields in [
                ("invalid", {"keep_awake": "false"}),
                ("foreign", {"keep_awake": False, "agent": "other"}),
            ]:
                await sockets[-1].send(json.dumps({"v": 1, "type": "power.set", "rid": rid, **fields}))
                await until(lambda rid=rid: reply(rid))
                assert reply(rid)["error"] and reply(rid)["enabled"] and reply(rid)["active"]
            monkeypatch.setattr(
                bridge.preferences,
                "put",
                lambda *_: (_ for _ in ()).throw(PermissionError("internal-error-must-not-leak")),
            )
            await sockets[-1].send(
                json.dumps({"v": 1, "type": "power.set", "keep_awake": False, "rid": "failure"})
            )
            await until(lambda: reply("failure"))
            assert "Не удалось сохранить" in reply("failure")["error"]
            assert "internal-error-must-not-leak" not in reply("failure")["error"]
            assert reply("failure")["enabled"] and reply("failure")["active"]
            assert bridge.relay.connected
            assert registry.path.read_bytes() == original_registration
            assert bridge.thread_id == "current" and bridge.project == tmp_path
        finally:
            await bridge.close()
        assert second.terminated == 1
