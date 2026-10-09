from test_project import project_controller

from bax_codex_light.permission_settings import choices
from bax_codex_light.project import ProjectController


async def test_busy_permission_choice_is_durable_and_applies_only_to_next_turn(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        a = owner.sessions["a"]
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        a.state, a.turn_id = "busy", "turn-a"
        template = dict(owner.template)
        await owner.on_frame({"type": "permissions.set", "session": "a", "mode": "read-only", "rid": "pick"})
        receipt = owner.relay.frames[-1]
        assert receipt["type"] == "permissions.settings" and receipt["rid"] == "pick"
        assert receipt["pending"] and receipt["current_mode"] == "custom"
        assert receipt["reviewer"] == "auto_review"
        assert owner.template == template
        restored = ProjectController(tmp_path, owner.registry, owner.state_path)
        assert restored.saved["a"]["permission_selection"] == "read-only"
        await owner.on_frame({"type": "run", "session": "a", "text": "Поправка", "cid": "steer"})
        steer = next(c for c in fake.calls if c["method"] == "turn/steer")
        assert "sandboxPolicy" not in steer["params"] and "approvalPolicy" not in steer["params"]
        assert a.permission_selection == "read-only"
        fake.threads["a"]["status"] = {"type": "idle"}
        a.state, a.turn_id = "ready", ""
        await owner.on_frame({"type": "run", "session": "a", "text": "Новая задача", "cid": "start"})
        start = next(c for c in fake.calls if c["method"] == "turn/start")
        assert start["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
        assert start["params"]["approvalPolicy"] == "on-request"
        assert "approvalsReviewer" not in start["params"]
        assert a.permission_selection is None
        applied = [f for f in owner.relay.frames if f["type"] == "permissions.settings"][-1]
        assert applied["current_mode"] == "read-only" and not applied["pending"]
        assert not fake.permission_configs.get("b")
        assert not any(
            c["method"] in {"turn/interrupt", "config/value/write", "config/batchWrite"} for c in fake.calls
        )


async def test_read_and_invalid_choices_never_change_codex(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.calls.clear()
        await owner.on_frame({"type": "permissions.get", "session": "a", "rid": "get"})
        assert not owner.relay.frames[-1]["pending"]
        assert all(
            c["method"] in {"thread/read", "thread/resume", "configRequirements/read"} for c in fake.calls
        )
        resume = next(c for c in fake.calls if c["method"] == "thread/resume")
        assert resume["params"] == {"threadId": "a", "excludeTurns": True}
        for session, mode in [
            ("b", "full-access"),
            ("foreign", "read-only"),
            ("a", "invalid"),
            ("a", {}),
            ("a", "full-access"),
        ]:
            await owner.on_frame({"type": "permissions.set", "session": session, "mode": mode, "rid": "bad"})
            assert owner.relay.frames[-1]["type"] == "permissions.settings"
            assert owner.relay.frames[-1]["error"]
            assert owner.sessions["a"].permission_selection is None
        assert not fake.permission_configs


async def test_managed_requirements_reject_full_access_and_named_profiles(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        for requirements in [
            {"allowedSandboxModes": ["read-only"], "allowedApprovalPolicies": ["on-request"]},
            {"allowedPermissionProfiles": {"safe": {}}},
        ]:
            fake.requirements = requirements
            await owner.on_frame(
                {
                    "type": "permissions.set",
                    "session": "a",
                    "mode": "full-access",
                    "confirm_full_access": True,
                }
            )
            assert owner.relay.frames[-1]["error"]
            assert owner.sessions["a"].permission_selection is None


async def test_explicit_full_access_and_return_to_current_mode(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.on_frame(
            {"type": "permissions.set", "session": "a", "mode": "full-access", "confirm_full_access": True}
        )
        assert owner.sessions["a"].permission_selection == "full-access"
        assert not fake.permission_configs
        await owner.on_frame({"type": "run", "session": "a", "text": "Задача", "cid": "start"})
        start = next(c for c in fake.calls if c["method"] == "turn/start")
        assert start["params"]["sandboxPolicy"] == {"type": "dangerFullAccess"}
        assert start["params"]["approvalPolicy"] == "never"
        await owner.on_frame({"type": "permissions.set", "session": "a", "mode": "read-only"})
        assert owner.sessions["a"].permission_selection == "read-only"
        await owner.on_frame(
            {"type": "permissions.set", "session": "a", "mode": "full-access", "confirm_full_access": True}
        )
        assert owner.sessions["a"].permission_selection is None


def test_empty_managed_allowlist_permits_no_modes():
    assert not any(item["allowed"] for item in choices({"allowedSandboxModes": []}))


async def test_native_codex_applies_all_modes_without_invoking_a_model(tmp_path):
    import asyncio
    import os
    import shutil
    import socket
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    from bax_codex_light.appserver import AppServer
    from bax_codex_light.permission_settings import PRESETS, mode_of, options

    if not shutil.which("codex"):
        pytest.skip("Нужен Codex CLI для проверки настоящего app-server")
    home = tmp_path / "isolated-codex"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local test; no model invoked"}}')

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        (home / "config.toml").write_text(
            'model = "test-permissions"\nmodel_provider = "local_test"\n'
            '[model_providers.local_test]\nname = "Local permission test"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            "supports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n"
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"ws://127.0.0.1:{port}"
        env = {"PATH": os.environ["PATH"], "CODEX_HOME": str(home)}
        with (tmp_path / "native.log").open("w") as log:
            process = await asyncio.to_thread(
                subprocess.Popen,
                ["codex", "app-server", "--listen", endpoint],
                env=env,
                stdout=log,
                stderr=log,
            )
            app = AppServer(endpoint, timeout=10)
            try:
                for _ in range(100):
                    try:
                        await app.open()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
                requirements = await app.permission_requirements()
                assert all(item["allowed"] for item in choices(requirements))
                thread = await app.new_thread(project, {})
                for mode in PRESETS:
                    await app.start_turn(thread["id"], "Проверка без действий", mode, **options(mode))
                    for _ in range(100):
                        current = await app.read_thread(thread["id"], project)
                        if current["status"]["type"] != "active":
                            break
                        await asyncio.sleep(0.05)
                    await app.resume_thread(thread["id"], project)
                    assert mode_of(app.configurations[thread["id"]]) == mode
            finally:
                await app.close()
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)
                await asyncio.to_thread(server.shutdown)
                await asyncio.to_thread(worker.join, timeout=2)
