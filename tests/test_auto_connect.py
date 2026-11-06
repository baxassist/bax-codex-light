import pytest
from test_project import project_controller

from bax_codex_light.appserver import RPCError
from bax_codex_light.lifecycle import register_event
from bax_codex_light.registry import Registry


async def test_hook_registration_selects_first_only_and_is_idempotent(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        first = await owner.session_opened("a")
        assert first == {"registered": True, "thread_id": "a", "selected": "a"}
        await owner.session_opened("b")
        assert owner.selected == "a"
        before = sum(c["method"] == "thread/resume" for c in fake.calls)
        await owner.session_opened("b")
        assert sum(c["method"] == "thread/resume" for c in fake.calls) == before
        assert set(owner.sessions) == {"a", "b"}
        with pytest.raises(RPCError):
            await owner.session_opened("foreign")
        assert owner.selected == "a"
        assert not any(
            c["method"] in {"thread/start", "turn/start", "account/login/start"} for c in fake.calls
        )


async def test_new_session_after_logout_login_keeps_selected_and_old_queue(tmp_path):
    from test_account_switching import model_calls, run_frame

    async with project_controller(tmp_path) as (fake, owner):
        await owner.session_opened("a")
        frame = run_frame()
        await owner.sessions["a"].accept_run(frame)
        fake.account = {"account": None, "requiresOpenaiAuth": True}
        await owner.sessions["a"]._drain()
        fake.account = {
            "account": {"type": "chatgpt", "email": None, "planType": "pro"},
            "requiresOpenaiAuth": True,
            "workspaceRouting": {
                "chatgptAccountId": "account-b",
                "backendOrigin": "https://chatgpt.com",
                "accountRoutingOverride": "NO_CONSTRAINT",
            },
        }
        await owner.session_opened("b")
        await owner.sessions["a"]._drain()
        assert frame["client_id"] in owner.sessions["a"].outbox
        assert not model_calls(fake)
        await owner.sessions["b"].on_frame(run_frame())
        assert len(model_calls(fake)) == 1
        assert model_calls(fake)[0]["params"]["threadId"] == "b"
        assert owner.selected == "a"


async def test_hooks_ignore_unregistered_projects_and_compaction(tmp_path):
    registry = Registry(tmp_path / "registrations.json")
    event = {"session_id": "a", "cwd": str(tmp_path), "hook_event_name": "SessionStart", "source": "startup"}
    assert (await register_event(event, registry))["reason"] == "needs_registration"
    event["source"] = "compact"
    assert (await register_event(event, registry))["reason"] == "ignored_source"
    assert not registry.path.exists()


async def test_hook_retries_readiness_without_resuming_unloaded_thread(tmp_path, monkeypatch):
    import asyncio

    import bax_codex_light.lifecycle as lifecycle

    async with project_controller(tmp_path) as (fake, owner):
        fake.threads["b"]["status"] = {"type": "notLoaded"}
        calls = 0
        attempted = asyncio.Event()

        async def ensure(_client):
            return tmp_path

        async def request(_directory, method, **params):
            nonlocal calls
            calls += 1
            assert method == "session_opened"
            try:
                return await owner.session_opened(params["thread_id"])
            except RPCError:
                attempted.set()
                raise

        monkeypatch.setattr(lifecycle.ProjectClient, "ensure_controller", ensure)
        monkeypatch.setattr(lifecycle, "request", request)
        event = {"hook_event_name": "SessionStart", "source": "fork", "session_id": "b", "cwd": str(tmp_path)}
        job = asyncio.create_task(register_event(event, owner.registry, owner.endpoint, readiness_seconds=3))
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            assert not any(c["method"] == "thread/resume" for c in fake.calls)
            fake.threads["b"]["status"] = {"type": "idle"}
            assert (await job)["registered"]
            assert calls >= 2
            assert sum(c["method"] == "thread/resume" for c in fake.calls) == 1
        finally:
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)


async def test_controller_never_reuses_other_backend(tmp_path):
    from test_project import ProjectApp

    from bax_codex_light.controller import ProjectClient, request

    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        first = ProjectClient(tmp_path, registry, "a", endpoint)
        directory = await first.ensure_controller()
        try:
            original = await request(directory, "status")
            other = ProjectClient(tmp_path, registry, "a", "ws://127.0.0.1:1")
            with pytest.raises(RPCError, match="другому AppServer"):
                await other.ensure_controller()
            assert (await request(directory, "status"))["controller_pid"] == original["controller_pid"]
        finally:
            await first.close()
            await request(directory, "shutdown")


async def test_reopen_restores_subscription_without_selecting_over_other_session(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.session_opened("a")
        await owner.session_opened("b")
        owner.sessions["b"].state = "offline"
        before = sum(c["method"] == "thread/resume" for c in fake.calls)
        await owner.session_opened("b")
        assert owner.sessions["b"].state != "offline"
        assert sum(c["method"] == "thread/resume" for c in fake.calls) == before + 1
        assert owner.selected == "a"
