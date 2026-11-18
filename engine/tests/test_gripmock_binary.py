"""gripmock Linux 平台 wheel 构建、解析与完整性校验。"""
from __future__ import annotations

import hashlib
import io
import json
import platform
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from astsight.mock import gripmock_binary

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import build_gripmock_wheels  # noqa: E402


def _archive(tmp_path: Path) -> Path:
    archive = tmp_path / build_gripmock_wheels.ASSETS["amd64"].archive_name
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in (
            ("gripmock", b"\x7fELF-fake-gripmock"),
            ("LICENSE", b"MIT License\n"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name == "gripmock" else 0o644
            bundle.addfile(member, io.BytesIO(data))
    return archive


def _distribution(tmp_path: Path, *, version: str = "3.23.8") -> SimpleNamespace:
    binary = tmp_path / gripmock_binary.BINARY_ENTRY
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELF-installed-gripmock")
    manifest = {
        "schema": 1,
        "artifact": gripmock_binary.DISTRIBUTION_NAME,
        "version": version,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "size": binary.stat().st_size,
        "platform": "linux",
        "architecture": "amd64",
        "source_commit": gripmock_binary.GRIPMOCK_SOURCE_COMMIT,
    }
    manifest_path = tmp_path / gripmock_binary.MANIFEST_ENTRY
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    files = [
        PurePosixPath(gripmock_binary.BINARY_ENTRY),
        PurePosixPath(gripmock_binary.MANIFEST_ENTRY),
    ]
    return SimpleNamespace(
        files=files,
        locate_file=lambda item: tmp_path / item,
    )


def _linux_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")


def test_build_wheel_has_platform_tag_binary_mode_manifest_and_licenses(
    tmp_path: Path,
) -> None:
    binary = b"\x7fELF-gripmock"
    wheel = build_gripmock_wheels.build_wheel(
        binary,
        b"MIT License\n",
        architecture="amd64",
        out_dir=tmp_path / "dist",
    )

    assert wheel.name.endswith(
        "-3.23.8-py3-none-manylinux_2_17_x86_64.whl"
    )
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert gripmock_binary.BINARY_ENTRY in names
        assert gripmock_binary.MANIFEST_ENTRY in names
        assert "astsight_gripmock_bin/licenses/LICENSE" in names
        assert "astsight_gripmock_bin/licenses/NOTICE" in names
        assert any(name.endswith("/RECORD") for name in names)
        manifest = json.loads(
            archive.read(gripmock_binary.MANIFEST_ENTRY)
        )
        assert manifest["version"] == "3.23.8"
        assert manifest["platform"] == "linux"
        assert manifest["architecture"] == "amd64"
        assert manifest["goarch"] == "amd64"
        assert manifest["sha256"] == hashlib.sha256(binary).hexdigest()
        assert manifest["source_commit"] == gripmock_binary.GRIPMOCK_SOURCE_COMMIT
        info = archive.getinfo(gripmock_binary.BINARY_ENTRY)
        assert info.external_attr >> 16 & 0o777 == 0o755
        wheel_metadata = next(
            name for name in names if name.endswith(".dist-info/WHEEL")
        )
        assert b"Tag: py3-none-manylinux_2_17_x86_64" in archive.read(
            wheel_metadata
        )


def test_build_wheel_selects_arm64_tag(tmp_path: Path) -> None:
    wheel = build_gripmock_wheels.build_wheel(
        b"\x7fELF-arm64",
        b"MIT License\n",
        architecture="arm64",
        out_dir=tmp_path,
    )

    assert wheel.name.endswith(
        "-3.23.8-py3-none-manylinux_2_17_aarch64.whl"
    )


def test_read_release_archive_verifies_pinned_archive_digest(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    binary, license_text = build_gripmock_wheels.read_release_archive(
        archive,
        verify_digest=False,
    )

    assert binary == b"\x7fELF-fake-gripmock"
    assert license_text == b"MIT License\n"
    with pytest.raises(build_gripmock_wheels.BuildError, match="sha256"):
        build_gripmock_wheels.read_release_archive(archive)


def test_resolver_loads_verified_wheel_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _distribution(tmp_path)
    manifest = json.loads(
        (tmp_path / gripmock_binary.MANIFEST_ENTRY).read_text("utf-8")
    )
    _linux_amd64(monkeypatch)
    monkeypatch.setitem(
        gripmock_binary.EXPECTED_BINARY_SHA256,
        "amd64",
        manifest["sha256"],
    )
    monkeypatch.setattr(
        gripmock_binary.importlib.metadata,
        "distribution",
        lambda _name: distribution,
    )

    resolved = gripmock_binary.resolve_gripmock_binary()

    assert resolved.path == tmp_path / gripmock_binary.BINARY_ENTRY
    assert resolved.architecture == "amd64"
    assert resolved.sha256 == manifest["sha256"]
    assert resolved.source == "wheel"


def test_resolver_rejects_version_checksum_and_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _distribution(tmp_path)
    manifest_path = tmp_path / gripmock_binary.MANIFEST_ENTRY
    original_manifest = json.loads(manifest_path.read_text("utf-8"))
    _linux_amd64(monkeypatch)
    monkeypatch.setitem(
        gripmock_binary.EXPECTED_BINARY_SHA256,
        "amd64",
        original_manifest["sha256"],
    )
    monkeypatch.setattr(
        gripmock_binary.importlib.metadata,
        "distribution",
        lambda _name: distribution,
    )

    manifest_path.write_text(
        json.dumps({**original_manifest, "version": "3.23.7"}),
        encoding="utf-8",
    )
    with pytest.raises(gripmock_binary.GripMockBinaryError) as version_error:
        gripmock_binary.resolve_gripmock_binary()
    assert version_error.value.reason == "mock_binary_version_mismatch"

    manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    binary = tmp_path / gripmock_binary.BINARY_ENTRY
    binary.write_bytes(b"\x7fELF-tampered")
    with pytest.raises(gripmock_binary.GripMockBinaryError) as checksum_error:
        gripmock_binary.resolve_gripmock_binary()
    assert checksum_error.value.reason == "mock_binary_checksum_mismatch"

    manifest_path.write_text(
        json.dumps({**original_manifest, "source_commit": "bad"}),
        encoding="utf-8",
    )
    with pytest.raises(gripmock_binary.GripMockBinaryError) as commit_error:
        gripmock_binary.resolve_gripmock_binary()
    assert commit_error.value.reason == "mock_binary_manifest_invalid"


def test_resolver_rejects_platform_outside_linux_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(gripmock_binary.GripMockBinaryError) as error:
        gripmock_binary.resolve_gripmock_binary()

    assert error.value.code == "mock_binary_missing"
    assert error.value.reason == "unsupported_platform"
