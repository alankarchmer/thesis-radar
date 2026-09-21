import pytest

from thesis_radar.config import DEFAULT_MODEL, Config, ConfigError, Workspace, load_config, resolve_workspace
from thesis_radar.lock import LockHeld, exclusive_lock


def test_workspace_resolution_prefers_explicit_then_env_then_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_HOME", str(tmp_path / "env"))
    assert resolve_workspace(str(tmp_path / "explicit")).root == (tmp_path / "explicit").resolve()
    assert resolve_workspace(None).root == (tmp_path / "env").resolve()
    monkeypatch.delenv("RADAR_HOME")
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(None).root == tmp_path.resolve()


def test_ensure_layout_creates_folders(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    for folder in (ws.inbox, ws.failed, ws.archive, ws.thesis_dir):
        assert folder.is_dir()
    assert ws.failed == tmp_path / "inbox" / "_failed"


def test_missing_config_uses_defaults(tmp_path):
    assert load_config(tmp_path / "config.yaml") == Config()
    assert Config().model == DEFAULT_MODEL == "jev-1.13.0"
    assert Config().concurrency == 16


def test_config_values_are_read(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("edgar_email: me@example.com\nconcurrency: 4\nmax_cost_per_run: 5\n", encoding="utf-8")
    config = load_config(path)
    assert (config.edgar_email, config.concurrency, config.max_cost_per_run) == ("me@example.com", 4, 5)


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("colour: red\n", "unknown keys"),
        ("concurrency: many\n", "wrong type"),
        ("concurrency: 0\n", ">= 1"),
        ("model: jev-latest\n", "pin a versioned model"),
    ],
)
def test_bad_config_is_rejected(tmp_path, text, fragment):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=fragment):
        load_config(path)


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "radar.lock"
    with exclusive_lock(path):
        with pytest.raises(LockHeld):
            with exclusive_lock(path):
                pass
    with exclusive_lock(path):
        pass
