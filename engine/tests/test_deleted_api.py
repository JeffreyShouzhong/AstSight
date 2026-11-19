"""被删除接口的变更感知：base 版本路由与当前路由对比。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from astsight.changes.deleted import deleted_apis
from astsight.inventory import inventory_routes

_APP = """
from fastapi import FastAPI

app = FastAPI()

@app.get("/keep")
def keep():
    return {"ok": True}

@app.get("/removed")
def removed():
    return {"ok": True}
"""

_APP_WITHOUT_REMOVED = """
from fastapi import FastAPI

app = FastAPI()

@app.get("/keep")
def keep():
    return {"ok": True}
"""


def _git(project: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    completed = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


def test_deleted_route_is_reported(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "test")
    app = project / "app.py"
    app.write_text(_APP, encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    app.write_text(_APP_WITHOUT_REMOVED, encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "remove endpoint")

    current, _audit, _client, _notes = inventory_routes(
        project, codegraph="off"
    )
    result = deleted_apis(
        project,
        base="HEAD~1",
        current_routes=current,
        codegraph="off",
    )
    assert result["checked"] is True, result
    assert [item["endpoint"] for item in result["deleted"]] == [
        "GET /removed"
    ]
    assert result["deleted"][0]["handler"]
    assert result["deleted"][0]["file"] == "app.py"


def test_missing_base_is_explicit(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init")
    result = deleted_apis(
        project,
        base="no-such-branch",
        current_routes=[],
        codegraph="off",
    )
    assert result["checked"] is False
    assert result["warnings"]
