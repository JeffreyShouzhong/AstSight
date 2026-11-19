"""被删除接口的变更感知：从 base 版本重建路由清单并与当前对比。

使用 `git worktree` 检出 base 的临时副本，跑同一套路由发现；只读，不改工作区。
扫描失败时显式返回 warning，不把“没检出”当成“没有删除”。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..inventory import inventory_routes
from ..profile.openapi import normalize_path


def deleted_apis(
    project: str | Path,
    *,
    base: str,
    current_routes: list[dict[str, Any]],
    codegraph: str = "auto",
    codegraph_module_root: str | None = None,
    openapi_source: str | None = None,
) -> dict[str, Any]:
    root = Path(project).resolve()
    result: dict[str, Any] = {
        "checked": False,
        "base": base,
        "deleted": [],
        "warnings": [],
    }
    if not base:
        result["warnings"].append("deleted_api_base_missing")
        return result
    revision = _merge_base(root, base)
    if revision is None:
        result["warnings"].append("deleted_api_base_unresolved")
        return result
    result["revision"] = revision
    worktree = Path(tempfile.mkdtemp(prefix="astsight-base-"))
    created = False
    try:
        added = _git(
            root,
            "worktree",
            "add",
            "--detach",
            str(worktree),
            revision,
        )
        if added.returncode != 0:
            result["warnings"].append(
                "deleted_api_worktree_failed:"
                + (added.stderr or added.stdout or "")[:300]
            )
            return result
        created = True
        base_openapi = _base_openapi(root, worktree, openapi_source)
        module_root = codegraph_module_root
        if module_root:
            candidate = Path(module_root)
            if not candidate.is_absolute():
                module_root = str(worktree / candidate)
        routes, _audit, _client, notes = inventory_routes(
            worktree,
            codegraph=codegraph,
            codegraph_module_root=module_root,
            full_index=False,
            openapi_source=base_openapi,
        )
        if notes:
            result["warnings"].extend(
                f"deleted_api_scan:{item}" for item in notes[:10]
            )
        current = {
            _key(item.get("method"), item.get("path"))
            for item in current_routes
            if item.get("method") and item.get("path")
        }
        deleted = []
        for route in routes:
            method = str(route.get("method") or "").upper()
            path = str(route.get("path") or "")
            if not method or not path:
                continue
            if _key(method, path) in current:
                continue
            deleted.append(
                {
                    "endpoint": f"{method} {path}",
                    "method": method,
                    "path": path,
                    "handler": route.get("handler") or "",
                    "file": route.get("file") or "",
                    "line": route.get("line") or 0,
                    "source": route.get("source") or "",
                }
            )
        result["deleted"] = sorted(
            deleted, key=lambda item: (item["method"], item["path"])
        )
        result["checked"] = True
        return result
    except Exception as exc:
        result["warnings"].append(
            f"deleted_api_scan_failed:{type(exc).__name__}:{exc}"[:500]
        )
        return result
    finally:
        if created:
            _git(root, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)


def _merge_base(project: Path, base: str) -> str | None:
    result = _git(project, "merge-base", base, "HEAD")
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = _git(project, "rev-parse", "--verify", base)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _base_openapi(
    project: Path, worktree: Path, openapi_source: str | None
) -> str | None:
    if not openapi_source:
        return None
    path = Path(openapi_source)
    if not path.is_absolute():
        path = project / path
    try:
        relative = path.resolve().relative_to(project)
    except (OSError, ValueError):
        return None
    candidate = worktree / relative
    return str(candidate) if candidate.is_file() else None


def _key(method: Any, path: Any) -> tuple[str, str]:
    return (
        str(method or "").upper(),
        normalize_path(str(path or "")),
    )


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


__all__ = ["deleted_apis"]
