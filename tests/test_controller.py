import asyncio

import pytest
from conftest import until
from test_project import ProjectApp

from bax_codex_light.appserver import RPCError
from bax_codex_light.controller import ProjectClient, private_directory, request, runtime_path
from bax_codex_light.registry import Registry


async def test_two_mcps_share_one_owner_and_eof_keeps_selection(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        first = ProjectClient(tmp_path, registry, "a", endpoint)
        second = ProjectClient(tmp_path, registry, "b", endpoint)
        directory, same = await asyncio.gather(first.ensure_controller(), second.ensure_controller())
        assert directory == same
        try:
            await first.bind("a")
            pid = (await first.status())["controller_pid"]
            await second.start()
            await asyncio.wait_for(second.ready.wait(), 8)
            assert not second.error
            assert (await second.status())["thread_id"] == "a"  # Новый MCP не выбирает себя автоматически.
            await second.bind("b")
            assert (await first.status())["controller_pid"] == pid
            await first.close()
            await second.close()
            status = await request(directory, "status")
            assert status["thread_id"] == "b" and status["controller_pid"] == pid
            assert runtime_path(tmp_path, registry, "ws://localhost:1234") == directory
            assert (directory / "control.sock").stat().st_mode & 0o077 == 0
        finally:
            await request(directory, "shutdown")
            await until(lambda: not fake.connections)


def test_controller_rejects_public_directory_and_symlink(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="приватным"):
        private_directory(public)
    link = tmp_path / "link"
    link.symlink_to(public, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        private_directory(link)


async def test_missing_mcp_context_can_identify_caller_without_selecting_it(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        first = ProjectClient(tmp_path, registry, "a", endpoint)
        directory = await first.ensure_controller()
        try:
            await first.bind("a")
            observer = ProjectClient(None, registry, endpoint=endpoint)
            status = await observer.status("b")
            assert status["caller_thread_id"] == "b" and status["thread_id"] == "a"
            assert status["needs_thread"]
            with pytest.raises(ValueError, match="другому разговору"):
                await observer.resolve_question("q", "a")
            assert (await first.status())["thread_id"] == "a"
            assert not any(c["method"] == "thread/start" for c in fake.calls)
        finally:
            await observer.close()
            await first.close()
            await request(directory, "shutdown")


@pytest.mark.parametrize("busy", [False, True])
async def test_upgrade_preserves_selection_and_never_interrupts_busy_owner(tmp_path, monkeypatch, busy):
    import bax_codex_light.controller as controller

    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        client = ProjectClient(tmp_path, registry, "a", endpoint)
        directory = await client.ensure_controller()
        old_pid = (await request(directory, "status"))["controller_pid"]
        if busy:
            fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        await client.bind("a")
        original_request = controller.request
        stopped = asyncio.Event()

        async def older(directory, method, **params):
            result = await original_request(directory, method, **params)
            if method == "status" and result["controller_pid"] == old_pid:
                result["plugin_version"] = "0.4.0"
            if method == "shutdown":
                stopped.set()
            return result

        monkeypatch.setattr(controller, "request", older)
        try:
            if busy:
                with pytest.raises(RPCError, match="занят"):
                    await client.ensure_controller()
                assert (await original_request(directory, "status"))["controller_pid"] == old_pid
                await client.start()
                await until(lambda: client.error_code == "controller_update_pending")
                # Несколько занятых проверок не должны разгонять сетевой backoff:
                # между задачами может быть меньше секунды.
                await asyncio.sleep(1.1)
                assert (await original_request(directory, "status"))["controller_pid"] == old_pid
                fake.threads["a"]["status"] = {"type": "idle"}
                await fake.emit("thread/status/changed", {"threadId": "a", "status": {"type": "idle"}})
                await asyncio.wait_for(stopped.wait(), 0.8)
                await asyncio.wait_for(client.ready.wait(), 8)
                current = await original_request(directory, "status")
                assert current["controller_pid"] != old_pid
                assert current["thread_id"] == "a"
                assert not client.error
            else:
                await client.ensure_controller()
                current = await original_request(directory, "status")
                assert current["controller_pid"] != old_pid
                assert current["thread_id"] == "a"
        finally:
            await client.close()
            await original_request(directory, "shutdown")
            await until(lambda: not fake.connections)


async def test_late_exact_caller_starts_controller_and_preserves_saved_selection(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        first = ProjectClient(tmp_path, registry, "a", endpoint)
        directory = await first.ensure_controller()
        await first.bind("a")
        await request(directory, "shutdown")
        await until(lambda: not (directory / "control.sock").exists())
        observer = ProjectClient(None, registry, endpoint=endpoint)
        await observer.start()
        assert observer.task is None  # Как MCP, запущенный Codex без ID в окружении.
        try:
            await observer.status("b")
            assert observer.task is not None
            await asyncio.wait_for(observer.ready.wait(), 8)
            result = await observer.status()
            assert result["caller_thread_id"] == "b"
            assert result["thread_id"] == "a"
            assert not any(call["method"] == "thread/start" for call in fake.calls)
            with pytest.raises(ValueError, match="другому разговору"):
                await observer.status("a")
        finally:
            await first.close()
            await observer.close()
            await request(directory, "shutdown")


async def test_status_recovers_controller_lost_after_completed_start(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        client = ProjectClient(tmp_path, registry, "a", endpoint)
        await client.start()
        await asyncio.wait_for(client.ready.wait(), 8)
        directory = runtime_path(tmp_path, registry, endpoint)
        await client.bind("a")
        client.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.task
        await request(directory, "shutdown")
        await until(lambda: not (directory / "control.sock").exists())
        old_task = client.task
        try:
            await client.status()
            assert client.task is not old_task
            await asyncio.wait_for(client.ready.wait(), 8)
            assert (await client.status())["thread_id"] == "a"
            assert not any(call["method"] == "thread/start" for call in fake.calls)
        finally:
            await client.close()
            await request(directory, "shutdown")


async def test_mcp_recovers_lost_owner_without_manual_status_or_reattach(tmp_path, monkeypatch):
    import contextlib

    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        client = ProjectClient(tmp_path, registry, "a", endpoint)
        await client.bind("a")
        directory = runtime_path(tmp_path, registry, endpoint)
        old_pid = (await request(directory, "status"))["controller_pid"]
        discovered = asyncio.Event()
        original = client.discover

        async def observed():
            await original()
            if not client.error:
                discovered.set()

        monkeypatch.setattr(client, "discover", observed)
        try:
            await client.start()
            await asyncio.wait_for(discovered.wait(), 5)
            await request(directory, "shutdown")
            # Read the owner directly: a manual MCP status() must not be what repairs it.
            async with asyncio.timeout(4):
                while True:
                    try:
                        current = await request(directory, "status")
                        if current["controller_pid"] != old_pid:
                            break
                    except (OSError, RPCError):
                        pass
                    await asyncio.sleep(0.02)
            assert current["thread_id"] == "a"
            assert not any(call["method"] == "thread/start" for call in fake.calls)
        finally:
            await client.close()
            with contextlib.suppress(OSError, RPCError):
                await request(directory, "shutdown")


async def test_simultaneous_upgrades_replace_owner_only_once(tmp_path, monkeypatch):
    import bax_codex_light.controller as controller

    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        first = ProjectClient(tmp_path, registry, "a", endpoint)
        second = ProjectClient(tmp_path, registry, "b", endpoint)
        await first.bind("a")
        directory = runtime_path(tmp_path, registry, endpoint)
        original = controller.request
        old_pid = (await original(directory, "status"))["controller_pid"]
        stopped = []

        async def older(directory, method, **params):
            result = await original(directory, method, **params)
            if method == "status" and result["controller_pid"] == old_pid:
                result["plugin_version"] = "0.4.0"
            if method == "shutdown":
                stopped.append(result)
            return result

        monkeypatch.setattr(controller, "request", older)
        try:
            await asyncio.gather(first.ensure_controller(), second.ensure_controller())
            current = await original(directory, "status")
            assert current["controller_pid"] != old_pid
            assert current["thread_id"] == "a"
            assert len(stopped) == 1
        finally:
            await first.close()
            await second.close()
            await original(directory, "shutdown")


async def test_broken_candidate_cannot_disconnect_previous_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import bax_codex_light.controller as controller

    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        client = ProjectClient(tmp_path, registry, "a", endpoint)
        await client.bind("a")
        directory = runtime_path(tmp_path, registry, endpoint)
        original = controller.request
        old_pid = (await original(directory, "status"))["controller_pid"]

        async def older(directory, method, **params):
            result = await original(directory, method, **params)
            if method == "status":
                result["plugin_version"] = "0.4.0"
            return result

        monkeypatch.setattr(controller, "request", older)
        monkeypatch.setattr(controller.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
        try:
            with pytest.raises(RPCError, match="действующий контроллер сохранён"):
                await client.ensure_controller()
            current = await original(directory, "status")
            assert current["controller_pid"] == old_pid and current["thread_id"] == "a"
        finally:
            await client.close()
            await original(directory, "shutdown")
