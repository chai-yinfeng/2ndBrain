from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import time
from pathlib import Path

from typer.testing import CliRunner

from secondbrain import ops, schedule
from secondbrain.cli import app
from secondbrain.pipeline.run import RunReport


def test_plist_pins_workspace_without_copying_credentials(tmp_path, monkeypatch):
    data = tmp_path / "Library With Space" / "data"
    config = tmp_path / "config"
    env = tmp_path / "secrets.env"
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setenv("SB_CONFIG_DIR", str(config))
    monkeypatch.setenv("SB_ENV_FILE", str(env))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-not-for-plist")
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    payload = schedule.build_plist(time(7), tmp_path, data / "logs", "/bin/uv")
    assert b"test-secret-not-for-plist" not in payload
    environment = plistlib.loads(payload)["EnvironmentVariables"]
    assert environment["SB_DATA_DIR"] == str(data.resolve())
    assert environment["SB_CONFIG_DIR"] == str(config.resolve())
    assert environment["SB_ENV_FILE"] == str(env.resolve())
    assert environment["SB_LLM_BACKEND"] == "fake"


def test_scheduled_status_distinguishes_completed_empty_and_wrong_workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setenv("SB_SCHEDULED_RUN", "1")
    monkeypatch.setattr(ops, "run_once", lambda *a, **kw: RunReport())
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert json.loads((data / "scheduled-run.json").read_text())["status"] == "empty"
    monkeypatch.setattr(
        ops, "run_once", lambda *a, **kw: RunReport(fetch_failures=1, fetch_added=2)
    )
    assert CliRunner().invoke(app, ["run"]).exit_code == 0
    assert json.loads((data / "scheduled-run.json").read_text())["status"] == "partial"
    path = schedule.plist_path()
    assert isinstance(path, Path) and path.parent.is_dir()
    path.write_bytes(schedule.build_plist(time(7), tmp_path, data / "logs", "/bin/uv"))

    def fake_launchctl(args, **kwargs):
        return subprocess.CompletedProcess(args, 0)

    status = schedule.status(fake_launchctl)
    assert status.loaded and status.workspace_matches and status.last_result["status"] == "partial"
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "different-data"))
    status = schedule.status(fake_launchctl)
    assert status.loaded and not status.workspace_matches and status.last_result is None
    assert status.data_dir == str(data.resolve())
