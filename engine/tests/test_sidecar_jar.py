"""Dubbo sidecar jar 解析与 wheel 清单校验。"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from astsight.sidecar import package as sidecar_package
from astsight.sidecar.dubbo import DubboSidecarError, locate_sidecar_jar

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import build_sidecar_wheel  # noqa: E402

VERSION = "0.1.0.dev0"
JAR_VERSION = "0.1.0-SNAPSHOT"
JAR_ENTRY = "astsight_dubbo_sidecar/sidecar/astsight-dubbo-sidecar.jar"
MANIFEST_ENTRY = "astsight_dubbo_sidecar/manifest.json"


def _fake_jar(path: Path, version: str = JAR_VERSION) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\r\n"
            f"Implementation-Version: {version}\r\n"
            "Main-Class: io.astsight.sidecar.dubbo.SidecarMain\r\n\r\n",
        )
    return path


def _manifest(jar: Path, **overrides) -> dict:
    payload = {
        "schema": 1,
        "artifact": "astsight-dubbo-sidecar",
        "version": VERSION,
        "jar_version": JAR_VERSION,
        "protocol": 1,
        "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
        "size": jar.stat().st_size,
    }
    payload.update(overrides)
    return payload


class _FakeDist:
    def __init__(self, root: Path):
        self.root = root
        self.files = [PurePosixPath(JAR_ENTRY), PurePosixPath(MANIFEST_ENTRY)]

    def locate_file(self, entry: PurePosixPath) -> Path:
        return self.root / entry


def _wheel_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "site-packages"
    jar = _fake_jar(root / JAR_ENTRY)
    manifest = root / MANIFEST_ENTRY
    manifest.parent.mkdir(parents=True, exist_ok=True)
    return root, jar, manifest


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_env_override_wins_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _fake_jar(tmp_path / "custom.jar")
    monkeypatch.setenv("ASTSIGHT_DUBBO_SIDECAR_JAR", str(jar))
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert reason is None
    assert resolved is not None
    assert resolved.path == jar
    assert resolved.source == "env"


def test_missing_env_path_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ASTSIGHT_DUBBO_SIDECAR_JAR", str(tmp_path / "missing.jar")
    )
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert resolved is None
    assert reason == "sidecar_jar_missing"


def test_wheel_manifest_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, jar, manifest = _wheel_layout(tmp_path)
    _write_manifest(manifest, _manifest(jar))
    monkeypatch.delenv("ASTSIGHT_DUBBO_SIDECAR_JAR", raising=False)
    monkeypatch.setattr(
        sidecar_package.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(root),
    )
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert reason is None
    assert resolved is not None
    assert resolved.source == "wheel"
    assert resolved.sha256 == hashlib.sha256(jar.read_bytes()).hexdigest()


def test_wheel_checksum_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, jar, manifest = _wheel_layout(tmp_path)
    _write_manifest(manifest, _manifest(jar, sha256="0" * 64))
    monkeypatch.delenv("ASTSIGHT_DUBBO_SIDECAR_JAR", raising=False)
    monkeypatch.setattr(
        sidecar_package.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(root),
    )
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert resolved is None
    assert reason == "sidecar_jar_checksum_mismatch"


def test_wheel_manifest_missing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _jar, _manifest_path = _wheel_layout(tmp_path)
    monkeypatch.delenv("ASTSIGHT_DUBBO_SIDECAR_JAR", raising=False)
    monkeypatch.setattr(
        sidecar_package.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(root),
    )
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert resolved is None
    assert reason == "sidecar_jar_manifest_missing"


def test_version_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _fake_jar(tmp_path / "other.jar", version="9.9.9")
    monkeypatch.setenv("ASTSIGHT_DUBBO_SIDECAR_JAR", str(jar))
    resolved, reason = sidecar_package.resolve_sidecar_jar(
        required_version=VERSION
    )
    assert resolved is None
    assert reason == "sidecar_jar_version_mismatch"


def test_locate_raises_sidecar_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ASTSIGHT_DUBBO_SIDECAR_JAR", str(tmp_path / "missing.jar")
    )
    with pytest.raises(DubboSidecarError) as error:
        locate_sidecar_jar()
    assert error.value.code == "sidecar_jar_missing"


def test_build_wheel_contains_manifest_and_record(tmp_path: Path) -> None:
    jar = _fake_jar(tmp_path / "astsight-dubbo-sidecar.jar")
    wheel = build_sidecar_wheel.build_wheel(jar, out_dir=tmp_path / "dist")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert JAR_ENTRY in names
        assert MANIFEST_ENTRY in names
        assert any(name.endswith("/RECORD") for name in names)
        manifest = json.loads(archive.read(MANIFEST_ENTRY))
        assert manifest["protocol"] == 1
        assert manifest["version"] == VERSION
        assert manifest["sha256"] == hashlib.sha256(jar.read_bytes()).hexdigest()
    assert (jar.with_name(jar.name + ".manifest.json")).is_file()
