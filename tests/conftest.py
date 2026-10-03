from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from websockets.asyncio.server import serve

from bax_codex_light import __version__


def thread(project, *, state="idle", thread_id="current"):
    return {
        "id": thread_id,
        "sessionId": thread_id,
        "cliVersion": "0.160.0",
        "createdAt": 1,
        "updatedAt": 1,
        "cwd": str(project),
        "ephemeral": False,
        "modelProvider": "openai",
        "preview": "",
        "source": "cli",
        "status": {"type": state, **({"activeFlags": []} if state == "active" else {})},
        "turns": [],
    }


def entry(item_id, kind="agentMessage", text="Ответ"):
    item = {"id": item_id, "type": kind, "text": text}
    if kind == "userMessage":
        item = {"id": item_id, "type": kind, "content": [{"type": "text", "text": text}]}
    return {"item": item, "turnId": "turn", "startedAtMs": 1, "completedAtMs": 2}


class FakeApp:
    def __init__(self, project):
        self.project = project
        self.state = "idle"
        self.calls = []
        self.responses = []
        self.connections = set()
        self.items = [entry("a", text="Последний ответ"), entry("u", "userMessage", "Задача")]
        self.started = asyncio.Event()

    async def handle(self, ws):
        self.connections.add(ws)
        try:
            async for raw in ws:
                message = json.loads(raw)
                if "method" not in message:
                    self.responses.append(message)
                    continue
                self.calls.append(message)
                method = message["method"]
                if "id" not in message:
                    continue
                if method == "initialize":
                    result = {"userAgent": "fake/0.160.0"}
                elif method == "thread/read":
                    result = {"thread": thread(self.project, state=self.state)}
                elif method == "thread/resume":
                    result = {
                        "thread": thread(self.project, state=self.state),
                        "cwd": str(self.project),
                        "model": "test",
                        "modelProvider": "openai",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sandbox": {"type": "readOnly"},
                    }
                elif method == "thread/items/list":
                    result = {"data": self.items, "nextCursor": None, "backwardsCursor": None}
                elif method == "turn/start":
                    self.state = "active"
                    self.started.set()
                    result = {"turn": {"id": "turn", "items": [], "status": "inProgress"}}
                elif method == "turn/steer":
                    result = {"turnId": message["params"]["expectedTurnId"]}
                elif method == "test/error":
                    await ws.send(json.dumps({"id": message["id"], "error": {"code": -1, "message": "test"}}))
                    continue
                else:
                    result = {}
                await ws.send(json.dumps({"id": message["id"], "result": result}))
        finally:
            self.connections.discard(ws)

    async def emit(self, method, params, request_id=None):
        if request_id is None and (model := NOTIFICATION_MODELS.get(method)):
            model.model_validate(params)
        payload = {"method": method, "params": params}
        if request_id is not None:
            payload["id"] = request_id
        for ws in list(self.connections):
            await ws.send(json.dumps(payload))

    @asynccontextmanager
    async def running(self):
        async with serve(self.handle, "127.0.0.1", 0) as server:
            yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def installed_plugin(tmp_path):
    if sys.platform not in {"darwin", "linux"} or not shutil.which("codex"):
        pytest.skip("Нужен Mac или Linux с Codex CLI")
    market = tmp_path / "Каталог с пробелами"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "plugins",
        market / "plugins",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    catalog = market / ".agents/plugins"
    catalog.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[1] / ".agents/plugins/marketplace.json", catalog)
    home = tmp_path / "isolated-codex"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('model = "user-choice"\n[mcp_servers.other]\ncommand = "/usr/bin/true"\n')
    env = dict(os.environ, CODEX_HOME=str(home))

    def run(*args):
        result = subprocess.run(["codex", *args], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    run("plugin", "marketplace", "add", str(market), "--json")
    run("plugin", "add", "bax-codex@baxassist", "--json")
    run("plugin", "add", "bax-codex@baxassist", "--json")
    settings = tomllib.loads(config.read_text())
    assert settings["model"] == "user-choice"
    assert settings["mcp_servers"]["other"]["command"] == "/usr/bin/true"
    server = next(row for row in run("mcp", "list", "--json") if row["name"] == "bax_codex")
    plugin = home / f"plugins/cache/baxassist/bax-codex/{__version__}"
    assert Path(server["transport"]["cwd"]).resolve() == plugin
    assert (plugin / "skills/connect/SKILL.md").is_file()
    yield server["transport"]
