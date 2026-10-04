import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_codex_resolves_plugin_paths_and_preserves_user_settings(installed_plugin):
    assert (Path(installed_plugin["cwd"]) / "bootstrap.py").is_file()


def test_native_first_turn_waits_for_pending_plugin(installed_plugin, tmp_path):
    runtime = os.environ.get("BAX_TEST_PLUGIN_RUNTIME")
    if not runtime:
        pytest.skip("Нужно подготовленное окружение BAX_TEST_PLUGIN_RUNTIME; тест ничего не скачивает")
    plugin = Path(installed_plugin["cwd"])
    home = plugin.parents[4]
    manifest = plugin / ".mcp.json"
    settings = json.loads(manifest.read_text())
    settings["mcpServers"]["bax_codex"]["env"] = {"BAX_CODEX_PLUGIN_DATA": runtime}
    manifest.write_text(json.dumps(settings))
    bootstrap = plugin / "bootstrap.py"
    source = bootstrap.read_text()
    # Холодный запуск дольше штатного grace (1 с), без скачивания зависимостей.
    source = source.replace("def main() -> None:\n", "def main() -> None:\n    time.sleep(2.5)\n")
    bootstrap.write_text(source)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"catalog captured; no model invoked"}}')

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        config = home / "config.toml"
        config.write_text(
            'model = "test-catalog"\nmodel_provider = "catalog_test"\n'
            '[model_providers.catalog_test]\nname = "Local catalog test"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            "supports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n"
            '[plugins."bax-codex@baxassist"]\nenabled = true\n'
        )
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        env["CODEX_HOME"] = str(home)
        started = time.monotonic()
        try:
            result = subprocess.run(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--color",
                    "never",
                    "-C",
                    str(tmp_path),
                    "Проверка каталога инструментов; никаких действий не выполняй.",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert requests, result.stderr
    assert time.monotonic() - started >= 2.5
    tools = json.dumps(requests[0]["tools"], ensure_ascii=False)
    for name in ("bax_status", "bax_attach", "bax_connect", "bax_resolve_question"):
        assert name in tools, f"{name} отсутствует в первом запросе Codex: {result.stderr}"
