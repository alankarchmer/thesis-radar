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
    assert ws.thesis_path("ACME") == tmp_path / "thesis" / "ACME.yaml"


def test_missing_config_uses_defaults(tmp_path):
    assert load_config(tmp_path / "config.yaml") == Config()
    assert Config().model == DEFAULT_MODEL == "jev-1.13.0"
    assert Config().concurrency == 16
    assert "10-K/A" in Config().edgar_forms and "6-K" in Config().peer_forms


def test_config_values_are_read(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "edgar_email: me@example.com\nconcurrency: 4\nmax_cost_per_run: 5\nedgar_forms: [10-k, 8-K]\n"
        "rejudge_window_days: 30\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert (config.edgar_email, config.concurrency, config.max_cost_per_run) == ("me@example.com", 4, 5)
    assert config.edgar_forms == ("10-K", "8-K") and config.rejudge_window_days == 30


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("colour: red\n", "unknown keys"),
        ("concurrency: many\n", "wrong type"),
        ("concurrency: 0\n", ">= 1"),
        ("model: jev-latest\n", "pin a versioned model"),
        ("edgar_forms: 10-K\n", "list of form names"),
        ("serve_port: 80\n", "serve_port"),
    ],
)
def test_bad_config_is_rejected(tmp_path, text, fragment):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=fragment):
        load_config(path)


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "radar.lock"
    with exclusive_lock(path), pytest.raises(LockHeld), exclusive_lock(path):
        pass
    with exclusive_lock(path):
        pass
