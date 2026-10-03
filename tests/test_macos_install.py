import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from bax_codex_light.install_macos import install


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("codex"), reason="Нужен Mac с Codex CLI")
def test_real_codex_plugin_install_in_isolated_home(tmp_path, monkeypatch):
    app = Path(__file__).resolve().parents[1] / "dist/Bax Codex.app"
    if not app.is_dir():
        pytest.skip("Сначала macos/build.sh")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text('model = "user-choice"\n[mcp_servers.other]\ncommand = "/usr/bin/true"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    root = tmp_path / "Свой каталог с пробелами"
    result = install(app, root=root)
    assert result["installed"]
    assert install(app, root=root)["installed"]
    settings = tomllib.loads(config.read_text())
    assert settings["model"] == "user-choice"
    assert settings["mcp_servers"]["other"]["command"] == "/usr/bin/true"
    manifests = list(codex_home.glob("plugins/cache/**/.mcp.json"))
    assert len(manifests) == 1
    server = json.loads(manifests[0].read_text())["mcpServers"]["bax_codex"]
    assert server["command"] == str(app / "Contents/Resources/agent/bax-codex-light")
    assert server["args"] == ["serve", "--auto-project"]
