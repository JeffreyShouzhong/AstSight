"""随包 gripmock 二进制的定位与完整性校验。

生产路径只接受已安装的 ``astsight-gripmock-bin`` 平台 wheel；不查 PATH，
也不在运行期联网下载。``--gripmock-path`` 的调试覆盖由调用方显式处理。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRIPMOCK_VERSION = "3.23.8"
GRIPMOCK_SOURCE_COMMIT = "871d06f802573d6ca731f84331a1a18b2cc15188"
DISTRIBUTION_NAME = "astsight-gripmock-bin"
IMPORT_NAME = "astsight_gripmock_bin"
BINARY_ENTRY = f"{IMPORT_NAME}/bin/gripmock"
MANIFEST_ENTRY = f"{IMPORT_NAME}/manifest.json"

EXPECTED_BINARY_SHA256 = {
    "amd64": "3899c1d58ef36af3c439e98812ec5a0a6860bab9cda56ad361c98eef6e70ea51",
    "arm64": "c3361204bdb3642f2291788a4e0ad0374cf8ac5e57e6ab41b71bb63f2e7b2b38",
}


class GripMockBinaryError(RuntimeError):
    """请求的随包二进制不可用；对外统一归类为 ``mock_binary_missing``。"""

    code = "mock_binary_missing"

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class GripMockBinary:
    path: Path
    version: str
    sha256: str
    size: int
    platform: str
    architecture: str
    source_commit: str
    source: str = "wheel"

    def manifest_fragment(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "size": self.size,
            "platform": self.platform,
            "architecture": self.architecture,
            "source_commit": self.source_commit,
            "source": self.source,
        }


def host_gripmock_architecture() -> str:
    """返回受支持的 GOARCH；矩阵外平台显式拒绝。"""
    if not sys.platform.startswith("linux"):
        raise GripMockBinaryError(
            "unsupported_platform",
            sys.platform,
        )
    machine = platform.machine().strip().lower()
    architecture = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if architecture is None:
        raise GripMockBinaryError(
            "unsupported_architecture",
            machine or "unknown",
        )
    return architecture


def resolve_gripmock_binary(
    *,
    required_version: str = GRIPMOCK_VERSION,
) -> GripMockBinary:
    """从已安装 wheel 解析二进制并校验版本、架构、大小和 sha256。"""
    architecture = host_gripmock_architecture()
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        raise GripMockBinaryError(
            "gripmock_distribution_missing",
            f"install {DISTRIBUTION_NAME}=={required_version}",
        ) from None

    binary_path, manifest_path = _wheel_paths(distribution)
    manifest = _read_manifest(manifest_path)
    _validate_manifest(
        manifest,
        binary_path=binary_path,
        expected_version=required_version,
        expected_architecture=architecture,
    )
    return GripMockBinary(
        path=binary_path,
        version=required_version,
        sha256=str(manifest["sha256"]).lower(),
        size=int(manifest["size"]),
        platform="linux",
        architecture=architecture,
        source_commit=str(manifest["source_commit"]),
    )


def _wheel_paths(distribution: Any) -> tuple[Path, Path]:
    files = list(getattr(distribution, "files", None) or [])
    binary_entry = next(
        (
            item
            for item in files
            if item.as_posix().endswith(BINARY_ENTRY)
        ),
        None,
    )
    if binary_entry is None:
        raise GripMockBinaryError("gripmock_binary_entry_missing", BINARY_ENTRY)
    manifest_entry = next(
        (
            item
            for item in files
            if item.as_posix().endswith(MANIFEST_ENTRY)
        ),
        None,
    )
    if manifest_entry is None:
        raise GripMockBinaryError("mock_binary_manifest_missing", MANIFEST_ENTRY)
    binary = Path(distribution.locate_file(binary_entry))
    manifest = Path(distribution.locate_file(manifest_entry))
    if not binary.is_file():
        raise GripMockBinaryError("gripmock_binary_missing", str(binary))
    return binary, manifest


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GripMockBinaryError(
            "mock_binary_manifest_invalid",
            str(path),
        ) from None
    if not isinstance(payload, dict):
        raise GripMockBinaryError(
            "mock_binary_manifest_invalid",
            str(path),
        )
    return payload


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    binary_path: Path,
    expected_version: str,
    expected_architecture: str,
) -> None:
    if (
        manifest.get("schema") != 1
        or manifest.get("artifact") != DISTRIBUTION_NAME
    ):
        raise GripMockBinaryError("mock_binary_manifest_invalid")
    if str(manifest.get("version") or "").strip() != expected_version:
        raise GripMockBinaryError(
            "mock_binary_version_mismatch",
            f"expected={expected_version} actual={manifest.get('version')}",
        )
    if manifest.get("platform") != "linux":
        raise GripMockBinaryError(
            "mock_binary_platform_mismatch",
            str(manifest.get("platform") or ""),
        )
    if manifest.get("architecture") != expected_architecture:
        raise GripMockBinaryError(
            "mock_binary_platform_mismatch",
            "expected="
            f"{expected_architecture} actual={manifest.get('architecture')}",
        )
    source_commit = str(manifest.get("source_commit") or "").strip().lower()
    if len(source_commit) != 40 or any(
        char not in "0123456789abcdef" for char in source_commit
    ):
        raise GripMockBinaryError("mock_binary_manifest_invalid", "source_commit")
    if source_commit != GRIPMOCK_SOURCE_COMMIT:
        raise GripMockBinaryError(
            "mock_binary_version_mismatch",
            f"source_commit={source_commit}",
        )

    declared_size = manifest.get("size")
    if not isinstance(declared_size, int) or declared_size < 1:
        raise GripMockBinaryError("mock_binary_manifest_invalid", "size")
    if binary_path.stat().st_size != declared_size:
        raise GripMockBinaryError("mock_binary_checksum_mismatch", "size")

    declared_digest = str(manifest.get("sha256") or "").strip().lower()
    actual_digest = _sha256(binary_path)
    if actual_digest != declared_digest:
        raise GripMockBinaryError("mock_binary_checksum_mismatch", "sha256")
    expected_digest = EXPECTED_BINARY_SHA256.get(expected_architecture)
    if not expected_digest or expected_digest == "TO_BE_FILLED":
        raise GripMockBinaryError(
            "mock_binary_manifest_invalid",
            f"missing pinned digest:{expected_architecture}",
        )
    if actual_digest != expected_digest:
        raise GripMockBinaryError(
            "mock_binary_checksum_mismatch",
            f"pinned sha256:{expected_architecture}",
        )


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


__all__ = [
    "BINARY_ENTRY",
    "DISTRIBUTION_NAME",
    "EXPECTED_BINARY_SHA256",
    "GRIPMOCK_SOURCE_COMMIT",
    "GRIPMOCK_VERSION",
    "GripMockBinary",
    "GripMockBinaryError",
    "MANIFEST_ENTRY",
    "host_gripmock_architecture",
    "resolve_gripmock_binary",
]
