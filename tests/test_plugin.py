from pathlib import Path


def test_codex_resolves_plugin_paths_and_preserves_user_settings(installed_plugin):
    assert (Path(installed_plugin["cwd"]) / "bootstrap.py").is_file()
