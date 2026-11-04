from pathlib import Path

import pytest
from typer.testing import CliRunner

from secondbrain import config
from secondbrain.cli import app

runner = CliRunner()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 data 目录与 .env，避免读到开发者本地的真实配置。"""
    data = tmp_path / "data"
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SB_LLM_BACKEND", raising=False)
    return data


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_without_key_warns_but_passes(isolated_env: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY 未设置" in result.output
    assert "SB_LLM_BACKEND = deepseek" in result.output
    assert "data 目录可写" in result.output
    assert isolated_env.is_dir()


def test_doctor_with_key_does_not_print_value(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-test-should-never-be-printed"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY 已设置" in result.output
    assert secret not in result.output


def test_doctor_reads_key_from_env_file(
    isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('DEEPSEEK_API_KEY="sk-from-file"\n', encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY 已设置" in result.output
    assert "sk-from-file" not in result.output


def test_doctor_fails_on_unknown_backend(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_LLM_BACKEND", "nope")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "未知后端" in result.output


def test_doctor_fails_when_data_dir_not_writable(
    isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("SB_DATA_DIR", str(blocker / "data"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "data 目录不可写" in result.output
