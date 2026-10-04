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
            await until(lambda: second.task.done())
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

        async def older(directory, method, **params):
            result = await original_request(directory, method, **params)
            if method == "status" and result["controller_pid"] == old_pid:
                result["plugin_version"] = "0.4.0"
            return result

        monkeypatch.setattr(controller, "request", older)
        try:
            if busy:
                with pytest.raises(RPCError, match="занят"):
                    await client.ensure_controller()
                assert (await original_request(directory, "status"))["controller_pid"] == old_pid
                await fake.emit("thread/status/changed", {"threadId": "a", "status": {"type": "idle"}})
                await asyncio.sleep(0.1)
            else:
                await client.ensure_controller()
                current = await original_request(directory, "status")
                assert current["controller_pid"] != old_pid
                assert current["thread_id"] == "a"
        finally:
            await client.close()
            await original_request(directory, "shutdown")
            await until(lambda: not fake.connections)
