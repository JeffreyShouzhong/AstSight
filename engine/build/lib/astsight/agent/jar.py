"""Java agent jar 解析与校验：环境变量 → 安装的 wheel → 源码树构建产物。

只有校验通过的 jar 才会被注入 `JAVA_TOOL_OPTIONS`。安装包（wheel）里的 jar
必须带 `manifest.json`（版本、协议号、sha256、大小），引擎逐项比对；源码树
里的 `mvn package` 产物没有 sidecar 时按“未校验的开发构建”接受，但版本仍须
匹配预期版本。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENT_PROTOCOL = 1
AGENT_VERSION = "0.1.0.dev0"
DISTRIBUTION_NAME = "astsight-java-agent"

_JAR_ENV = "ASTSIGHT_JAVA_AGENT_JAR"
_VERSION_ENV = "ASTSIGHT_JAVA_AGENT_REQUIRED_VERSION"
_IMPORT_NAME = "astsight_java_agent"
_WHEEL_JAR = f"{_IMPORT_NAME}/agent/astsight-agent.jar"
_WHEEL_MANIFEST = f"{_IMPORT_NAME}/manifest.json"
_JAR_DEFAULT = (
    Path(__file__).resolve().parents[3]
    / "java"
    / "astsight-agent"
    / "target"
    / "astsight-agent.jar"
)
_SNAPSHOT_RE = re.compile(r"^(.+)-SNAPSHOT$")


class AgentJarError(ValueError):
    """jar 或 manifest 无法解析；`reason` 直接进入注入告警后缀。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AgentJar:
    path: Path
    version: str
    source: str
    sha256: str | None = None


def normalize_agent_version(raw: str) -> str:
    """把 Maven 版本归一成 PEP 440 可表示的版本号。

    `0.1.0-SNAPSHOT` → `0.1.0.dev0`；`0.2-SNAPSHOT` → `0.2.0.dev0`。
    """
    text = raw.strip()
    match = _SNAPSHOT_RE.match(text)
    if not match:
        return text
    parts = match.group(1).split(".")
    if all(part.isdigit() for part in parts) and len(parts) < 3:
        parts += ["0"] * (3 - len(parts))
    return ".".join(parts) + ".dev0"


def expected_agent_version() -> str:
    override = os.environ.get(_VERSION_ENV, "").strip()
    return override or AGENT_VERSION


def resolve_agent_jar(*, required_version: str | None = None) -> tuple[AgentJar | None, str | None]:
    """返回 (jar, 失败原因)。失败原因为 None 时 jar 一定可用。"""
    expected = normalize_agent_version(required_version or expected_agent_version())

    configured = os.environ.get(_JAR_ENV, "").strip()
    if configured:
        # 显式覆盖只认这一个路径，缺失就报错，不静默回退到别的 jar。
        return _load(Path(configured), source="env", expected=expected, require_manifest=False)

    try:
        wheel = _wheel_paths(importlib.metadata.distribution(DISTRIBUTION_NAME))
    except importlib.metadata.PackageNotFoundError:
        wheel = None
    except AgentJarError as error:
        return None, error.reason
    if wheel is not None:
        jar_path, manifest_path = wheel
        if manifest_path is None:
            return None, "agent_jar_manifest_missing"
        return _load(
            jar_path,
            source="wheel",
            expected=expected,
            require_manifest=True,
            manifest_path=manifest_path,
        )

    return _load(_JAR_DEFAULT, source="repo", expected=expected, require_manifest=False)


def _wheel_paths(dist: Any) -> tuple[Path, Path | None]:
    """从已安装 distribution 里定位 jar 与 manifest（测试可传 fake dist）。"""
    files = list(getattr(dist, "files", None) or [])
    jar_entry = next((f for f in files if f.as_posix().endswith(_WHEEL_JAR)), None)
    if jar_entry is None:
        raise AgentJarError("agent_jar_missing")
    manifest_entry = next(
        (f for f in files if f.as_posix().endswith(_WHEEL_MANIFEST)), None
    )
    jar = Path(dist.locate_file(jar_entry))
    manifest = Path(dist.locate_file(manifest_entry)) if manifest_entry else None
    return jar, manifest


def _load(
    path: Path,
    *,
    source: str,
    expected: str,
    require_manifest: bool,
    manifest_path: Path | None = None,
) -> tuple[AgentJar | None, str | None]:
    if not path.is_file():
        return None, "agent_jar_missing"
    if any(char.isspace() for char in str(path)):
        return None, "agent_jar_path_has_space"
    try:
        entries = _jar_manifest(path)
    except AgentJarError as error:
        return None, error.reason
    jar_version = entries.get("Implementation-Version", "").strip()
    if not jar_version:
        return None, "agent_jar_unreadable"
    if normalize_agent_version(jar_version) != expected:
        return None, "agent_jar_version_mismatch"

    sidecar = manifest_path or path.with_name(path.name + ".manifest.json")
    if not sidecar.is_file():
        if require_manifest:
            return None, "agent_jar_manifest_missing"
        return AgentJar(path=path, version=expected, source=source), None
    try:
        manifest = _read_manifest(sidecar)
        digest = _sha256(path)
        _check_manifest(
            manifest,
            jar_version=jar_version,
            expected=expected,
            digest=digest,
            size=path.stat().st_size,
        )
    except AgentJarError as error:
        return None, error.reason
    return (
        AgentJar(path=path, version=expected, source=source, sha256=digest),
        None,
    )


def _jar_manifest(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile):
        raise AgentJarError("agent_jar_unreadable") from None
    entries: dict[str, str] = {}
    last_key: str | None = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith(" ") and last_key:
            entries[last_key] += line[1:]
            continue
        if ":" not in line:
            last_key = None
            continue
        key, value = line.split(":", 1)
        last_key = key.strip()
        entries[last_key] = value.strip()
    return entries


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AgentJarError("agent_jar_manifest_invalid") from None
    if not isinstance(parsed, dict):
        raise AgentJarError("agent_jar_manifest_invalid")
    return parsed


def _check_manifest(
    manifest: dict[str, Any],
    *,
    jar_version: str,
    expected: str,
    digest: str,
    size: int,
) -> None:
    if manifest.get("schema") != 1 or manifest.get("artifact") != DISTRIBUTION_NAME:
        raise AgentJarError("agent_jar_manifest_invalid")
    if str(manifest.get("jar_version") or "").strip() != jar_version:
        raise AgentJarError("agent_jar_version_mismatch")
    if normalize_agent_version(str(manifest.get("version") or "")) != expected:
        raise AgentJarError("agent_jar_version_mismatch")
    if manifest.get("protocol") != AGENT_PROTOCOL:
        raise AgentJarError("agent_jar_protocol_mismatch")
    if str(manifest.get("sha256") or "").lower() != digest:
        raise AgentJarError("agent_jar_checksum_mismatch")
    declared_size = manifest.get("size")
    if isinstance(declared_size, int) and declared_size != size:
        raise AgentJarError("agent_jar_size_mismatch")


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


__all__ = [
    "AGENT_PROTOCOL",
    "AGENT_VERSION",
    "DISTRIBUTION_NAME",
    "AgentJar",
    "AgentJarError",
    "expected_agent_version",
    "normalize_agent_version",
    "resolve_agent_jar",
]
