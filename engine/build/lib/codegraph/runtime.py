"""CodeGraph 运行时定位与可选安装。

CodeGraph 以平台 wheel `astsight-codegraph-bin` 分发，内含固定版本 CodeGraph
与自包含 Node 运行时；引擎优先从包资源解析，不依赖 PATH、npm 或网络。

用户级缓存仅作为开发/离线回退，让 `codegraph install` 装一次、所有项目复用：

    python -m astsight codegraph install
    python -m astsight codegraph status

定位顺序（`resolve_module_root`）：
1. 显式参数 `--codegraph-module-root` / 构造函数参数；
2. `ASTSIGHT_CODEGRAPH_MODULE_ROOT` 环境变量；
3. 用户级缓存目录（本模块的 `default_module_root`）。

只有前三步都没有时返回 `None`，由 worker 回退到 bridge 同级目录；
桥接层仍会显式报“包未安装”，不会静默当成空图谱。
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CODEGRAPH_PACKAGE = "@colbymchenry/codegraph"
CODEGRAPH_VERSION = "1.6.2"
PACKAGE_DIR = Path("node_modules") / "@colbymchenry" / "codegraph"
PACKAGED_DISTRIBUTION = "astsight-codegraph-bin"
PACKAGED_IMPORT = "astsight_codegraph_bin"


def default_module_root() -> Path:
    """用户级持久化目录；可用 `ASTSIGHT_CODEGRAPH_HOME` 覆盖。"""
    override = os.environ.get("ASTSIGHT_CODEGRAPH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(base) / "AstSight" / "codegraph").resolve()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (Path(base) / "astsight" / "codegraph").resolve()


def installed_version(root: str | Path | None = None) -> str | None:
    """读取已安装的 CodeGraph 版本；未安装返回 None。"""
    package_json = Path(root or default_module_root()) / PACKAGE_DIR / "package.json"
    if not package_json.is_file():
        return None
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    return str(version) if version else None


def is_installed(root: str | Path | None = None) -> bool:
    return packaged_runtime() is not None or installed_version(root) is not None


def packaged_runtime() -> dict[str, Any] | None:
    """从平台 wheel 解析 Node 与 CodeGraph；未安装或损坏时返回 None。

    平台包自包含 Node 运行时，不依赖 PATH、npm 或网络。
    """
    try:
        distribution = metadata.distribution(PACKAGED_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    files = list(getattr(distribution, "files", None) or [])
    manifest_entry = next(
        (
            item
            for item in files
            if item.as_posix().endswith(f"{PACKAGED_IMPORT}/manifest.json")
        ),
        None,
    )
    if manifest_entry is None:
        return None
    try:
        manifest = json.loads(
            Path(distribution.locate_file(manifest_entry)).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    node_rel = str(manifest.get("node_entry") or "")
    if not node_rel:
        return None
    node = Path(
        distribution.locate_file(f"{PACKAGED_IMPORT}/{node_rel}")
    )
    module_root = Path(
        distribution.locate_file(
            f"{PACKAGED_IMPORT}/{manifest.get('module_root') or 'node_modules'}"
        )
    )
    if not node.is_file() or not module_root.is_dir():
        return None
    return {
        "root": str(module_root),
        "node": str(node),
        "version": str(manifest.get("codegraph_version") or ""),
        "platform": str(manifest.get("platform") or ""),
        "source": "packaged",
    }


def resolve_module_root(explicit: str | Path | None = None) -> str | None:
    """按优先级解析 node_modules 所在目录。"""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    env = os.environ.get("ASTSIGHT_CODEGRAPH_MODULE_ROOT")
    if env:
        return str(Path(env).expanduser().resolve())
    packaged = packaged_runtime()
    if packaged is not None:
        return packaged["root"]
    managed = default_module_root()
    if installed_version(managed) is not None:
        return str(managed)
    return None


def resolve_node(explicit: str | Path | None = None) -> str | None:
    """解析 Node 可执行文件：显式/env → 平台 wheel → PATH。"""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    env = os.environ.get("ASTSIGHT_NODE")
    if env and Path(env).is_file():
        return str(Path(env).expanduser().resolve())
    packaged = packaged_runtime()
    if packaged is not None:
        return packaged["node"]
    return shutil.which("node") or shutil.which("node.exe")


def _npm() -> str:
    found = shutil.which("npm") or shutil.which("npm.cmd")
    if not found:
        raise RuntimeError(
            "找不到 npm。CodeGraph 是 Node 库，需要先安装 Node.js >= 22.5（含 npm）。"
        )
    return found


def _node_version(node: str | None = None) -> str | None:
    executable = node or shutil.which("node") or shutil.which("node.exe")
    if not executable:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or proc.stderr).strip() or None


def _package_json(root: Path) -> None:
    path = root / "package.json"
    if path.exists():
        return
    payload = {
        "name": "astsight-codegraph-runtime",
        "private": True,
        "description": "AstSight managed CodeGraph runtime",
        "dependencies": {CODEGRAPH_PACKAGE: CODEGRAPH_VERSION},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install(
    root: str | Path | None = None,
    *,
    version: str = CODEGRAPH_VERSION,
    force: bool = False,
) -> dict[str, Any]:
    """把 CodeGraph 安装到持久化目录，返回安装结果。

    平台 wheel 已提供运行时时直接复用；`force=True` 才重新安装到持久化目录。
    已安装且版本一致时默认直接复用。
    """
    packaged = packaged_runtime()
    if packaged is not None and not force:
        return {
            "ok": True,
            "installed": True,
            "reused": True,
            "source": "packaged",
            "root": packaged["root"],
            "node": packaged["node"],
            "version": packaged["version"],
        }
    target = Path(root).expanduser().resolve() if root else default_module_root()
    existing = installed_version(target)
    if existing == version and not force:
        return {
            "ok": True,
            "installed": True,
            "reused": True,
            "root": str(target),
            "version": existing,
        }
    target.mkdir(parents=True, exist_ok=True)
    _package_json(target)
    spec = f"{CODEGRAPH_PACKAGE}@{version}"
    proc = subprocess.run(
        [
            _npm(),
            "install",
            "--prefix",
            str(target),
            "--no-audit",
            "--no-fund",
            "--save-exact",
            spec,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"CodeGraph 安装失败（exit {proc.returncode}）: "
            f"{(proc.stderr or proc.stdout).strip()[-2000:]}"
        )
    return {
        "ok": True,
        "installed": True,
        "reused": False,
        "root": str(target),
        "version": installed_version(target),
        "output": (proc.stdout or "").strip()[-2000:],
    }


def status(root: str | Path | None = None) -> dict[str, Any]:
    """返回运行时状态，供 CLI 与诊断使用。"""
    managed = Path(root).expanduser().resolve() if root else default_module_root()
    packaged = packaged_runtime()
    resolved = resolve_module_root(root)
    managed_version = installed_version(managed)
    version = packaged["version"] if packaged else managed_version
    node = resolve_node()
    return {
        "package": CODEGRAPH_PACKAGE,
        "pinned_version": CODEGRAPH_VERSION,
        "module_root": resolved,
        "managed_root": str(managed),
        "installed": version is not None,
        "installed_version": version,
        "source": packaged["source"] if packaged else (
            "managed" if managed_version else None
        ),
        "node": _node_version(node),
        "node_path": node,
        "npm": shutil.which("npm") or shutil.which("npm.cmd"),
    }
