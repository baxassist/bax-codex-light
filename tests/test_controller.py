import asyncio

import pytest
from conftest import until
from test_project import ProjectApp

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
