"""Dubbo 直连目标解析：来源校验、端点去重、服务映射与阻断。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.execute import dubbo_targets
from astsight.execute.dubbo_targets import (
    DubboTargetConfig,
    DubboTargetError,
    config_from_run,
    normalize_service_key,
    resolve_dubbo_targets,
)


def _profile(
    service: str,
    *,
    group: str = "",
    version: str = "",
    protocol: str = "dubbo",
    serialization: str = "hessian2",
    compatibility: str = "native",
    direct_url: str | None = None,
    port: int | None = 20880,
) -> dict:
    return {
        "transport": "dubbo",
        "service": service,
        "group": group,
        "version": version,
        "protocol": protocol,
        "serialization": serialization,
        "compatibility": compatibility,
        "direct_url": direct_url,
        "protocol_port": port,
        "file": "OrderServiceImpl.java",
        "line": 10,
    }


def _resolve(profiles, config=None, project=Path(".")):
    return resolve_dubbo_targets(
        profiles,
        config=config or DubboTargetConfig(),
        project=project,
    )


def test_explicit_code_url_wins(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880")
    ]
    result = _resolve(profiles, project=tmp_path)
    assert result.ok
    assert result.targets == {
        "com.acme.OrderService": "dubbo://10.0.0.5:20880"
    }
    assert result.services[0]["source"] == "code"
    assert result.services[0]["determinism"] == "exact"


def test_single_code_endpoint_is_shared(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880"),
        _profile("com.acme.PayService"),
    ]
    result = _resolve(profiles, project=tmp_path)
    assert result.ok
    assert result.targets == {
        "com.acme.OrderService": "dubbo://10.0.0.5:20880",
        "com.acme.PayService": "dubbo://10.0.0.5:20880",
    }
    assert result.services[1]["source"] == "code_shared"


def test_runtime_direct_endpoint_fills_unresolved(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService"),
        _profile("com.acme.PayService"),
    ]
    result = _resolve(
        profiles,
        DubboTargetConfig(direct_endpoint="dubbo://10.0.0.6:20881"),
        project=tmp_path,
    )
    assert result.ok
    assert set(result.targets.values()) == {"dubbo://10.0.0.6:20881"}
    assert {item["source"] for item in result.services} == {"direct"}


def test_two_endpoints_require_mapping(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880"),
        _profile("com.acme.PayService", direct_url="dubbo://10.0.0.6:20881"),
        _profile("com.acme.CartService"),
    ]
    result = _resolve(profiles, project=tmp_path)
    assert not result.ok
    assert any(
        item["code"] == "dubbo_target_unresolved"
        and item["service_key"] == "com.acme.CartService"
        for item in result.errors
    )


def test_service_mapping_with_group_version(tmp_path: Path) -> None:
    service = "com.acme.OrderService"
    profiles = [
        _profile(service, group="order", version="1.0.0"),
        _profile("com.acme.PayService"),
    ]
    key = normalize_service_key(service, "order", "1.0.0")
    result = _resolve(
        profiles,
        DubboTargetConfig(
            direct_endpoint="dubbo://10.0.0.7:20882",
            service_targets={key: "10.0.0.8:20880"},
        ),
        project=tmp_path,
    )
    assert result.ok
    assert result.targets[key] == "dubbo://10.0.0.8:20880"
    assert result.targets["com.acme.PayService"] == "dubbo://10.0.0.7:20882"


def test_mapping_cannot_override_code_without_flag(tmp_path: Path) -> None:
    service = "com.acme.OrderService"
    profiles = [
        _profile(service, direct_url="dubbo://10.0.0.5:20880"),
    ]
    config = DubboTargetConfig(
        service_targets={service: "dubbo://10.0.0.9:20880"}
    )
    result = _resolve(profiles, config, project=tmp_path)
    assert not result.ok
    assert any(
        item["code"] == "dubbo_target_override_required"
        for item in result.errors
    )


def test_mapping_can_override_with_flag(tmp_path: Path) -> None:
    service = "com.acme.OrderService"
    profiles = [
        _profile(service, direct_url="dubbo://10.0.0.5:20880"),
    ]
    config = DubboTargetConfig(
        service_targets={service: "dubbo://10.0.0.9:20880"},
        allow_override=True,
    )
    result = _resolve(profiles, config, project=tmp_path)
    assert result.ok
    assert result.targets[service] == "dubbo://10.0.0.9:20880"
    assert result.services[0]["source"] == "mapping_override"


def test_global_url_cannot_mask_multiple_endpoints(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880"),
        _profile("com.acme.PayService", direct_url="dubbo://10.0.0.6:20881"),
    ]
    result = _resolve(
        profiles,
        DubboTargetConfig(global_url="dubbo://10.0.0.5:20880"),
        project=tmp_path,
    )
    assert not result.ok
    assert any(
        item["code"] == "dubbo_global_url_ambiguous"
        for item in result.errors
    )


def test_hostname_target_is_rejected(tmp_path: Path) -> None:
    profiles = [_profile("com.acme.OrderService")]
    result = _resolve(
        profiles,
        DubboTargetConfig(direct_endpoint="dubbo://pay-service:20880"),
        project=tmp_path,
    )
    assert not result.ok
    assert any(item["code"] == "dubbo_target_invalid" for item in result.errors)


def test_direct_host_uses_each_protocol_port(tmp_path: Path) -> None:
    profiles = [
        _profile("com.acme.OrderService", port=20880),
        _profile("com.acme.PayService", port=20881),
    ]
    result = _resolve(
        profiles,
        DubboTargetConfig(direct_host="10.0.0.6"),
        project=tmp_path,
    )
    assert result.ok
    assert result.targets["com.acme.OrderService"] == "dubbo://10.0.0.6:20880"
    assert result.targets["com.acme.PayService"] == "dubbo://10.0.0.6:20881"


def test_source_commit_mismatch_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dubbo_targets,
        "_git",
        lambda project, *args: "abc123" if args[:2] == ("rev-parse", "HEAD") else "main",
    )
    result = _resolve(
        [_profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880")],
        DubboTargetConfig(source_commit="expected", source_branch="main"),
        project=tmp_path,
    )
    assert not result.ok
    assert any(item["code"] == "dubbo_source_mismatch" for item in result.errors)


def test_source_sha256_checks_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "sut.jar"
    artifact.write_bytes(b"built-by-ci")
    digest = hashlib.sha256(b"built-by-ci").hexdigest()
    result = _resolve(
        [_profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880")],
        DubboTargetConfig(
            source_sha256=digest,
            artifact_path=str(artifact),
        ),
        project=tmp_path,
    )
    assert result.ok
    assert result.source["verified"] is True


def test_missing_source_is_a_warning(tmp_path: Path) -> None:
    result = _resolve(
        [_profile("com.acme.OrderService", direct_url="dubbo://10.0.0.5:20880")],
        project=tmp_path,
    )
    assert result.ok
    assert any("dubbo_source_unverified" in item for item in result.warnings)


def test_config_from_run_merges_file_and_cli(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "targets": {
                    "com.acme.OrderService": "dubbo://10.0.0.5:20880"
                },
                "allow_override": False,
            }
        ),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        dubbo_targets=str(path),
        dubbo_target="dubbo://10.0.0.6:20881",
        dubbo_url=None,
        dubbo_host=None,
        dubbo_source_sha256=None,
        dubbo_source_commit=None,
        dubbo_source_branch=None,
        dubbo_artifact=None,
        dubbo_target_override=True,
    )
    config = config_from_run(run, project=tmp_path)
    assert config.direct_endpoint == "dubbo://10.0.0.6:20881"
    assert config.service_targets == {
        "com.acme.OrderService": "dubbo://10.0.0.5:20880"
    }
    assert config.allow_override is True


def test_service_key_group_version_is_order_independent() -> None:
    assert normalize_service_key("com.acme.OrderService", "g", "1") == (
        "com.acme.OrderService?group=g&version=1"
    )
    with pytest.raises(DubboTargetError):
        dubbo_targets._service_key_from_mapping(
            "com.acme.OrderService?foo=bar"
        )
