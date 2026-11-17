"""Dubbo sidecar jar 的解析与校验：环境变量 → 已安装 wheel → 源码树构建产物。

与 Java agent 同一套口径：wheel 内的 jar 必须带 `manifest.json`，逐项校验
版本、协议号、sha256 与大小；源码树里的 `mvn package` 产物没有清单时按
开发构建接受，但仍校验 jar 版本。
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

SIDECAR_PROTOCOL = 1
SIDECAR_VERSION = "0.1.0.dev0"
DISTRIBUTION_NAME = "astsight-dubbo-sidecar"

_JAR_ENV = "ASTSIGHT_DUBBO_SIDECAR_JAR"
_VERSION_ENV = "ASTSIGHT_DUBBO_SIDECAR_REQUIRED_VERSION"
_IMPORT_NAME = "astsight_dubbo_sidecar"
_WHEEL_JAR = f"{_IMPORT_NAME}/sidecar/astsight-dubbo-sidecar.jar"
_WHEEL_MANIFEST = f"{_IMPORT_NAME}/manifest.json"
_DEFAULT_JAR = (
    Path(__file__).resolve().parents[3]
    / "java"
    / "astsight-sidecar-dubbo"
    / "target"
    / "astsight-dubbo-sidecar.jar"
)
_SNAPSHOT_RE = re.compile(r"^(.+)-SNAPSHOT$")


class SidecarJarError(ValueError):
    """jar 或 manifest 无法解析；`reason` 直接进入 sidecar 错误码后缀。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SidecarJar:
    path: Path
    version: str
    source: str
    sha256: str | None = None


def normalize_sidecar_version(raw: str) -> str:
    text = raw.strip()
    match = _SNAPSHOT_RE.match(text)
    if not match:
        return text
    parts = match.group(1).split(".")
    if all(part.isdigit() for part in parts) and len(parts) < 3:
        parts += ["0"] * (3 - len(parts))
    return ".".join(parts) + ".dev0"


def expected_sidecar_version() -> str:
    override = os.environ.get(_VERSION_ENV, "").strip()
    return override or SIDECAR_VERSION


def resolve_sidecar_jar(
    *, required_version: str | None = None
) -> tuple[SidecarJar | None, str | None]:
    """返回 (jar, 失败原因)。失败原因为 None 时 jar 一定可用。"""
    expected = normalize_sidecar_version(
        required_version or expected_sidecar_version()
    )

    configured = os.environ.get(_JAR_ENV, "").strip()
    if configured:
        return _load(
            Path(configured),
            source="env",
            expected=expected,
            require_manifest=False,
        )

    try:
        wheel = _wheel_paths(importlib.metadata.distribution(DISTRIBUTION_NAME))
    except importlib.metadata.PackageNotFoundError:
        wheel = None
    except SidecarJarError as error:
        return None, error.reason
    if wheel is not None:
        jar_path, manifest_path = wheel
        if manifest_path is None:
            return None, "sidecar_jar_manifest_missing"
        return _load(
            jar_path,
            source="wheel",
            expected=expected,
            require_manifest=True,
            manifest_path=manifest_path,
        )

    return _load(
        _DEFAULT_JAR, source="repo", expected=expected, require_manifest=False
    )


def _wheel_paths(dist: Any) -> tuple[Path, Path | None]:
    files = list(getattr(dist, "files", None) or [])
    jar_entry = next(
        (item for item in files if item.as_posix().endswith(_WHEEL_JAR)), None
    )
    if jar_entry is None:
        raise SidecarJarError("sidecar_jar_missing")
    manifest_entry = next(
        (item for item in files if item.as_posix().endswith(_WHEEL_MANIFEST)), None
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
) -> tuple[SidecarJar | None, str | None]:
    if not path.is_file():
        return None, "sidecar_jar_missing"
    if any(char.isspace() for char in str(path)):
        return None, "sidecar_jar_path_has_space"
    try:
        entries = _jar_manifest(path)
    except SidecarJarError as error:
        return None, error.reason
    jar_version = entries.get("Implementation-Version", "").strip()
    if not jar_version:
        return None, "sidecar_jar_unreadable"
    if normalize_sidecar_version(jar_version) != expected:
        return None, "sidecar_jar_version_mismatch"

    manifest_file = manifest_path or path.with_name(path.name + ".manifest.json")
    if not manifest_file.is_file():
        if require_manifest:
            return None, "sidecar_jar_manifest_missing"
        return SidecarJar(path=path, version=expected, source=source), None
    try:
        manifest = _read_manifest(manifest_file)
        digest = _sha256(path)
        _check_manifest(
            manifest,
            jar_version=jar_version,
            expected=expected,
            digest=digest,
            size=path.stat().st_size,
        )
    except SidecarJarError as error:
        return None, error.reason
    return (
        SidecarJar(path=path, version=expected, source=source, sha256=digest),
        None,
    )


def _jar_manifest(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile):
        raise SidecarJarError("sidecar_jar_unreadable") from None
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
        raise SidecarJarError("sidecar_jar_manifest_invalid") from None
    if not isinstance(parsed, dict):
        raise SidecarJarError("sidecar_jar_manifest_invalid")
    return parsed


def _check_manifest(
    manifest: dict[str, Any],
    *,
    jar_version: str,
    expected: str,
    digest: str,
    size: int,
) -> None:
    if (
        manifest.get("schema") != 1
        or manifest.get("artifact") != DISTRIBUTION_NAME
    ):
        raise SidecarJarError("sidecar_jar_manifest_invalid")
    if str(manifest.get("jar_version") or "").strip() != jar_version:
        raise SidecarJarError("sidecar_jar_version_mismatch")
    if normalize_sidecar_version(str(manifest.get("version") or "")) != expected:
        raise SidecarJarError("sidecar_jar_version_mismatch")
    if manifest.get("protocol") != SIDECAR_PROTOCOL:
        raise SidecarJarError("sidecar_jar_protocol_mismatch")
    if str(manifest.get("sha256") or "").lower() != digest:
        raise SidecarJarError("sidecar_jar_checksum_mismatch")
    declared_size = manifest.get("size")
    if isinstance(declared_size, int) and declared_size != size:
        raise SidecarJarError("sidecar_jar_size_mismatch")


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


__all__ = [
    "DISTRIBUTION_NAME",
    "SIDECAR_PROTOCOL",
    "SIDECAR_VERSION",
    "SidecarJar",
    "SidecarJarError",
    "expected_sidecar_version",
    "normalize_sidecar_version",
    "resolve_sidecar_jar",
]
