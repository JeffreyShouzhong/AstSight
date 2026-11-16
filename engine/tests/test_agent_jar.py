"""Java agent jar 分发闭环：wheel 清单、校验和、版本比对与解析优先级。"""
from __future__ import annotations

import importlib.metadata
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from astsight.agent import jar as agent_jar

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import build_agent_wheel  # noqa: E402


def _fake_jar(path: Path, version: str = "0.1.0-SNAPSHOT") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\r\n"
            f"Implementation-Version: {version}\r\n"
            "Premain-Class: io.astsight.agent.core.AstSightAgent\r\n\r\n",
        )
    return path


class _FakeDist:
    """最小 distribution：只暴露 files + locate_file。"""

    def __init__(self, root: Path, names: list[str]) -> None:
        self.files = [PurePosixPath(name) for name in names]
        self._root = root

    def locate_file(self, path: PurePosixPath) -> Path:
        return self._root / path


def _install_wheel(tmp_path: Path, *, manifest: dict | None = None) -> _FakeDist:
    wheel = build_agent_wheel.build_wheel(
        _fake_jar(tmp_path / "built.jar"),
        version="0.1.0.dev0",
        out_dir=tmp_path / "dist",
    )
    root = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(root)
    if manifest is not None:
        path = root / build_agent_wheel.MANIFEST_ENTRY
        path.write_text(json.dumps(manifest), encoding="utf-8")
    return _FakeDist(
        root,
        [
            item.relative_to(root).as_posix()
            for item in root.rglob("*")
            if item.is_file()
        ],
    )


def _no_distribution(name: str):
    raise importlib.metadata.PackageNotFoundError(name)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTSIGHT_JAVA_AGENT_JAR", raising=False)
    monkeypatch.delenv("ASTSIGHT_JAVA_AGENT_REQUIRED_VERSION", raising=False)


def _use_dist(monkeypatch: pytest.MonkeyPatch, dist) -> None:
    def fake_distribution(name: str):
        if name != agent_jar.DISTRIBUTION_NAME:
            raise importlib.metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(agent_jar.importlib.metadata, "distribution", fake_distribution)


def test_normalize_agent_version_matches_repo_pom() -> None:
    assert agent_jar.normalize_agent_version("0.1.0-SNAPSHOT") == "0.1.0.dev0"
    assert agent_jar.normalize_agent_version("0.2-SNAPSHOT") == "0.2.0.dev0"
    assert agent_jar.normalize_agent_version("1.2.3") == "1.2.3"
    assert (
        agent_jar.normalize_agent_version(build_agent_wheel.maven_version())
        == agent_jar.AGENT_VERSION
    )


def test_wheel_jar_is_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    _use_dist(monkeypatch, _install_wheel(tmp_path))

    resolved, reason = agent_jar.resolve_agent_jar()

    assert reason is None
    assert resolved is not None
    assert resolved.source == "wheel"
    assert resolved.version == agent_jar.AGENT_VERSION
    assert resolved.sha256 and len(resolved.sha256) == 64
    assert resolved.path.name == "astsight-agent.jar"


def test_wheel_checksum_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    dist = _install_wheel(tmp_path)
    jar_path = tmp_path / "site-packages" / build_agent_wheel.JAR_ENTRY
    jar_path.write_bytes(_fake_jar(tmp_path / "tampered.jar").read_bytes() + b"x")
    _use_dist(monkeypatch, dist)

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason == "agent_jar_checksum_mismatch"


def test_wheel_version_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    dist = _install_wheel(tmp_path)
    manifest_path = tmp_path / "site-packages" / build_agent_wheel.MANIFEST_ENTRY
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _use_dist(monkeypatch, dist)

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason == "agent_jar_version_mismatch"


def test_wheel_requires_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    _use_dist(monkeypatch, _install_wheel(tmp_path, manifest={}))

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason in {"agent_jar_manifest_missing", "agent_jar_manifest_invalid"}


def test_env_override_wins_over_installed_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _use_dist(monkeypatch, _install_wheel(tmp_path))
    override = _fake_jar(tmp_path / "override.jar", "0.1.0.dev0")
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(override))

    resolved, reason = agent_jar.resolve_agent_jar()

    assert reason is None
    assert resolved is not None
    assert resolved.source == "env"
    assert resolved.path == override
    assert resolved.sha256 is None


def test_env_override_without_file_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _use_dist(monkeypatch, _install_wheel(tmp_path))
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(tmp_path / "missing.jar"))

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason == "agent_jar_missing"


def test_repo_build_without_sidecar_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr(agent_jar.importlib.metadata, "distribution", _no_distribution)
    repo_jar = _fake_jar(tmp_path / "repo.jar", "0.1.0.dev0")
    monkeypatch.setattr(agent_jar, "_JAR_DEFAULT", repo_jar)

    resolved, reason = agent_jar.resolve_agent_jar()

    assert reason is None
    assert resolved is not None
    assert resolved.source == "repo"
    assert resolved.sha256 is None


def test_jar_path_with_space_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    spaced = tmp_path / "with space"
    spaced.mkdir()
    monkeypatch.setenv(
        "ASTSIGHT_JAVA_AGENT_JAR",
        str(_fake_jar(spaced / "astsight-agent.jar", "0.1.0.dev0")),
    )

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason == "agent_jar_path_has_space"


def test_wrong_jar_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    stale = _fake_jar(tmp_path / "stale.jar", "0.0.9")
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(stale))

    resolved, reason = agent_jar.resolve_agent_jar()

    assert resolved is None
    assert reason == "agent_jar_version_mismatch"


def test_required_version_can_be_pinned_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_REQUIRED_VERSION", "0.1.0.dev0")
    monkeypatch.setenv(
        "ASTSIGHT_JAVA_AGENT_JAR",
        str(_fake_jar(tmp_path / "ok.jar", "0.1.0-SNAPSHOT")),
    )

    resolved, reason = agent_jar.resolve_agent_jar()

    assert reason is None
    assert resolved is not None
    assert resolved.version == "0.1.0.dev0"


def test_build_wheel_layout_and_record(tmp_path: Path) -> None:
    jar = _fake_jar(tmp_path / "astsight-agent.jar")
    wheel = build_agent_wheel.build_wheel(jar, out_dir=tmp_path / "dist")

    assert wheel.name == "astsight_java_agent-0.1.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(build_agent_wheel.MANIFEST_ENTRY))
        record = archive.read(
            "astsight_java_agent-0.1.0.dev0.dist-info/RECORD"
        ).decode("utf-8")
    assert {
        "astsight_java_agent/__init__.py",
        build_agent_wheel.JAR_ENTRY,
        build_agent_wheel.MANIFEST_ENTRY,
        "astsight_java_agent-0.1.0.dev0.dist-info/METADATA",
        "astsight_java_agent-0.1.0.dev0.dist-info/WHEEL",
        "astsight_java_agent-0.1.0.dev0.dist-info/RECORD",
    } <= names
    assert manifest["version"] == "0.1.0.dev0"
    assert manifest["jar_version"] == "0.1.0-SNAPSHOT"
    assert manifest["protocol"] == agent_jar.AGENT_PROTOCOL
    assert manifest["size"] == jar.stat().st_size
    assert "astsight_java_agent/agent/astsight-agent.jar,sha256=" in record
    assert "astsight_java_agent-0.1.0.dev0.dist-info/RECORD,," in record
    sidecar = jar.with_name(jar.name + ".manifest.json")
    assert json.loads(sidecar.read_text(encoding="utf-8"))["sha256"] == manifest["sha256"]
