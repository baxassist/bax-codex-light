import asyncio
import json
from uuid import uuid4

import pytest
from conftest import FakeApp

from bax_codex_light.bridge import Bridge
from bax_codex_light.registry import Registry


async def test_auto_project_uses_exact_thread_not_process_directory(tmp_path):
    project = tmp_path / "Сайт с пробелами"
    project.mkdir()
    fake = FakeApp(project)
    async with fake.running() as endpoint:
        bridge = Bridge(None, Registry(tmp_path / "private.json"), endpoint=endpoint)
        await bridge.bind("current")
        assert bridge.project == project
        assert bridge.status()["needs_registration"]
        with pytest.raises(ValueError):
            await bridge.bind("another-thread")
        await bridge.close()
    assert all(c["method"] != "turn/start" for c in fake.calls)


async def test_connect_keeps_project_identity_and_rotates_same_agent_key(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "private.json")
    bridge = Bridge(tmp_path, registry)
    agent, key_id, secret = str(uuid4()), str(uuid4()), "x" * 40
    key = f"{agent}:{key_id}:{secret}"
    with pytest.raises(ValueError):
        await bridge.connect(key)
    assert not registry.path.exists()

    async def no_start():
        pass

    monkeypatch.setattr(bridge, "start", no_start)
    bridge.thread_id = "current"
    response = await bridge.connect(key)
    assert secret not in json.dumps(response)
    installed = registry.get(tmp_path)
    await bridge.connect(key)
    assert registry.get(tmp_path).install_id == installed.install_id
    with pytest.raises(ValueError, match="другому агенту"):
        await bridge.connect(f"{uuid4()}:{uuid4()}:" + "y" * 40)
    assert registry.get(tmp_path) == installed
    old_task = asyncio.create_task(asyncio.sleep(60))
    bridge.task = old_task
    rotated_id = str(uuid4())
    await bridge.connect(f"{agent}:{rotated_id}:" + "z" * 40)
    assert old_task.cancelled()
    assert registry.get(tmp_path).key_id == rotated_id
    assert registry.get(tmp_path).agent == installed.agent
    assert bridge.thread_id == "current"
