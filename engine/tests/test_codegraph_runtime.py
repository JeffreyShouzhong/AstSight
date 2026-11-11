"""CodeGraph 运行时定位与持久化安装的单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codegraph import runtime


def _fake_install(root: Path, version: str = "1.6.2") -> Path:
    package = root / "node_modules" / "@colbymchenry" / "codegraph"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@colbymchenry/codegraph", "version": version}),
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("ASTSIGHT_CODEGRAPH_MODULE_ROOT", raising=False)
    monkeypatch.delenv("ASTSIGHT_CODEGRAPH_HOME", raising=False)


def test_explicit_root_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTSIGHT_CODEGRAPH_MODULE_ROOT", str(tmp_path / "env"))
    assert runtime.resolve_module_root(tmp_path / "explicit") == str(
        (tmp_path / "explicit").resolve()
    )


def test_env_root_used(monkeypatch, tmp_path):
    env_root = tmp_path / "env"
    monkeypatch.setenv("ASTSIGHT_CODEGRAPH_MODULE_ROOT", str(env_root))
    assert runtime.resolve_module_root() == str(env_root.resolve())


def test_managed_root_used_when_installed(monkeypatch, tmp_path):
    managed = _fake_install(tmp_path / "managed")
    monkeypatch.setenv("ASTSIGHT_CODEGRAPH_HOME", str(managed))
    assert runtime.installed_version() == "1.6.2"
    assert runtime.resolve_module_root() == str(managed.resolve())


def test_managed_root_ignored_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTSIGHT_CODEGRAPH_HOME", str(tmp_path / "missing"))
    assert runtime.installed_version() is None
    assert runtime.resolve_module_root() is None


def test_install_reuses_matching_version(monkeypatch, tmp_path):
    managed = _fake_install(tmp_path / "managed")
    monkeypatch.setattr(
        runtime, "_npm", lambda: (_ for _ in ()).throw(AssertionError("npm called"))
    )
    result = runtime.install(managed)
    assert result["ok"] and result["reused"]
    assert result["root"] == str(managed.resolve())
    assert result["version"] == "1.6.2"


def test_install_runs_pinned_npm_package(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    calls: list[list[str]] = []

    class Proc:
        returncode = 0
        stdout = "added 2 packages"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        (managed / "node_modules" / "@colbymchenry" / "codegraph").mkdir(
            parents=True, exist_ok=True
        )
        (
            managed / "node_modules" / "@colbymchenry" / "codegraph" / "package.json"
        ).write_text('{"version": "1.6.2"}', encoding="utf-8")
        return Proc()

    monkeypatch.setattr(runtime, "_npm", lambda: "npm")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    result = runtime.install(managed)
    assert result["ok"] and not result["reused"]
    assert "@colbymchenry/codegraph@1.6.2" in calls[0]
    assert "--prefix" in calls[0]


def test_status_reports_managed_state(monkeypatch, tmp_path):
    managed = _fake_install(tmp_path / "managed")
    monkeypatch.setenv("ASTSIGHT_CODEGRAPH_HOME", str(managed))
    monkeypatch.setattr(runtime, "_node_version", lambda node=None: "v24.0.0")
    state = runtime.status()
    assert state["installed"] is True
    assert state["installed_version"] == "1.6.2"
    assert state["module_root"] == str(managed.resolve())
