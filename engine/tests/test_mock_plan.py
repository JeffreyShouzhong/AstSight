"""Mock 计划（C3.5.5 P0）：校验、内嵌服务生命周期和 `run --mock-plan` 链路。"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from astsight.cases.draft import case_fingerprint
from astsight.cases.store import save_case_set
from astsight.mock import (
    MockPlanError,
    load_mock_plan,
    route_key_for,
    start_mock_plan,
)
from astsight.pipeline import RunConfig, run
from contracts import (
    Assertion,
    CaseEntry,
    CaseReview,
    CaseSet,
    CaseStatus,
    MockPlan,
    ReviewDecision,
    TestCase as CaseModel,
)

DEP_ID = "http:svc:pay-service GET /gateway/pay"


def _plan_payload(**overrides) -> dict:
    payload = {
        "sut_id": "pay-sut",
        "routes": [
            {
                "dep_id": DEP_ID,
                "method": "GET",
                "path_key": "/gateway/pay",
                "host": "pay-service",
                "target": "http://pay-service/gateway/pay",
                "branches": {
                    "success": {
                        "status": 200,
                        "body": {"code": 0, "message": "ok"},
                    }
                },
            }
        ],
        "injections": [
            {
                "dep_id": DEP_ID,
                "strategy": "url_override",
                "kind": "base_url",
                "property": "PAY_BASE_URL",
                "original": "http://pay-service",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _write_plan(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mock-plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_load_mock_plan_normalizes_route_and_computes_route_key(
    tmp_path: Path,
) -> None:
    payload = _plan_payload()
    payload["routes"][0]["path_key"] = "gateway//pay/"
    payload["routes"][0]["target"] = "http://pay-service/gateway/pay?trace=1"
    plan = load_mock_plan(_write_plan(tmp_path, payload))

    route = plan.routes[0]
    assert route.path_key == "/gateway/pay"
    assert route.target == "http://pay-service/gateway/pay"
    assert route.host == "pay-service"
    assert route.route_key == route_key_for(DEP_ID)
    assert route.strategy.value == "url_override"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda payload: payload["routes"].append(dict(payload["routes"][0])),
            "mock_route_conflict",
        ),
        (
            lambda payload: payload["routes"][0].update(
                {"target": "https://pay-service/gateway/pay"}
            ),
            "tls_not_supported",
        ),
        (
            lambda payload: payload["routes"][0].update({"method": "TRACE"}),
            "mock_method_invalid",
        ),
        (
            lambda payload: payload["routes"][0].update({"dep_id": "pay-service"}),
            "mock_dep_id_invalid",
        ),
        (
            lambda payload: payload["injections"][0].update(
                {"dep_id": "http:svc:other GET /other"}
            ),
            "mock_injection_unknown_dep",
        ),
        (
            lambda payload: payload["injections"][0].pop("property"),
            "mock_config_not_overridable",
        ),
        (
            lambda payload: payload["injections"][0].update(
                {"strategy": "unsupported", "reason": None}
            ),
            "mock_injection_invalid",
        ),
    ],
)
def test_load_mock_plan_rejects_invalid_plans(
    tmp_path: Path, mutate, code: str
) -> None:
    payload = _plan_payload()
    mutate(payload)
    with pytest.raises(MockPlanError) as caught:
        load_mock_plan(_write_plan(tmp_path, payload))
    assert caught.value.code == code


def test_plan_rejects_route_key_mismatch_and_oversized_file(
    tmp_path: Path,
) -> None:
    payload = _plan_payload()
    payload["routes"][0]["route_key"] = "rdeadbeef0000"
    with pytest.raises(MockPlanError) as caught:
        load_mock_plan(_write_plan(tmp_path, payload))
    assert caught.value.code == "mock_route_key_mismatch"

    oversized = tmp_path / "huge-plan.json"
    oversized.write_bytes(b"{" + b" " * (4 * 1024 * 1024) + b"}")
    with pytest.raises(MockPlanError) as caught:
        load_mock_plan(oversized)
    assert caught.value.code == "mock_plan_too_large"


def test_start_mock_plan_serves_route_and_emits_manifest_without_token(
    tmp_path: Path,
) -> None:
    payload = _plan_payload()
    payload["port"] = 0
    runtime = start_mock_plan(
        _write_plan(tmp_path, payload),
        history_path=tmp_path / "mock_calls.jsonl",
    )
    try:
        assert runtime.server.endpoint.startswith("http://127.0.0.1:")
        injection = runtime.injections[0]
        assert injection.injected == (
            f"{runtime.server.endpoint}/_dep/{route_key_for(DEP_ID)}"
        )
        assert injection.property == "PAY_BASE_URL"

        admin = {"Authorization": f"Bearer {runtime.token}"}
        created = httpx.post(
            f"{runtime.server.admin_endpoint}/_admin/sessions",
            headers=admin,
            json={
                "session_id": "plan-s1",
                "namespace": "pay-sut",
                "case_id": "TC-0001",
                "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
            },
            timeout=5,
        )
        assert created.status_code == 200, created.text
        # base_url 注入后 SUT 仍会追加原路径。
        called = httpx.get(
            injection.injected + "/gateway/pay",
            timeout=5,
        )
        assert called.status_code == 200
        assert called.json() == {"code": 0, "message": "ok"}

        manifest = runtime.manifest(run_id="run-1")
        document = runtime.injection_document()
        assert manifest["route_count"] == 1
        assert manifest["injections"][0]["injected"] == injection.injected
        assert document["unsupported_count"] == 0
        rendered = json.dumps([manifest, document], ensure_ascii=False)
        assert '"token"' not in rendered
        assert runtime.token not in rendered
        assert "PAY_BASE_URL" in rendered
    finally:
        runtime.stop()

    assert runtime.server.started is False


def _make_store(project: Path, *, endpoint: str, mock_bindings: dict[str, str]) -> Path:
    now = "2026-11-02T00:00:00+00:00"
    case = CaseModel(
        id="TC-0001",
        endpoint=endpoint,
        category="positive",
        method="POST",
        path="/pay",
        asserts=[
            Assertion(kind="status_code", op="eq", expect=200),
            Assertion(kind="body_field", target="code", op="eq", expect=0),
        ],
        source="test",
        mock_bindings=mock_bindings,
    )
    fingerprint = case_fingerprint(case, "pay_success")
    entry = CaseEntry(
        **case.model_dump(),
        case_key="pay_success",
        status=CaseStatus.APPROVED,
        version=1,
        fingerprint=fingerprint,
        review=CaseReview(
            decision=ReviewDecision.APPROVE,
            by="e2e",
            at=now,
            fingerprint=fingerprint,
        ),
        created_at=now,
        updated_at=now,
    )
    store = CaseSet(
        id="cs-plan-e2e",
        project=project.name,
        store_version=1,
        sequence=1,
        generated_at=now,
        updated_at=now,
        approved_at=now,
        approved_by="e2e",
        openapi={},
        cases=[entry],
        history=[],
        review_log=[],
        doc_versions={},
        requirements=[],
        uncovered_reasons={},
    )
    store_path = project / "caseset.json"
    save_case_set(store_path, store, backup=False)
    return store_path


SUT_APP = '''
import os

import httpx
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/pay")
def pay():
    base = os.environ["PAY_BASE_URL"].rstrip("/")
    response = httpx.get(base + "/gateway/pay", timeout=5)
    return {"code": response.json().get("code")}
'''


@contextlib.contextmanager
def _sut(project: Path, env: dict[str, str]):
    port = _free_port()
    log = project / "uvicorn.log"
    with open(log, "wb") as stream:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=project,
            env={**os.environ, **env},
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "SUT 启动失败:\n"
                    + log.read_text(encoding="utf-8", errors="replace")[-4000:]
                )
            try:
                if httpx.get(base_url + "/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise RuntimeError("SUT 启动超时")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_run_with_mock_plan_starts_injects_executes_and_stops(
    tmp_path: Path,
) -> None:
    project = tmp_path / "sut"
    project.mkdir()
    (project / "app.py").write_text(SUT_APP, encoding="utf-8")
    _make_store(project, endpoint="POST /pay", mock_bindings={DEP_ID: "success"})

    plan_port = _free_port()
    plan_path = _write_plan(
        project,
        {
            **_plan_payload(),
            "port": plan_port,
        },
    )
    injected = f"http://127.0.0.1:{plan_port}/_dep/{route_key_for(DEP_ID)}"

    with _sut(project, {"PAY_BASE_URL": injected}) as base_url:
        report = run(
            RunConfig(
                project=str(project),
                mode="cases",
                base_url=base_url,
                mock_plan=str(plan_path),
                codegraph="off",
                retries=0,
                collect_only=True,
                timeout=120,
            )
        )

    assert report["gate"]["verdict"] == "pass", report["gate"]
    assert report["summary"]["executed_cases"] == 1
    assert report["summary"]["final_pass"] == 1, report.get("failures")
    mock = report["mock"]
    assert mock["checked"] is True
    assert mock["calls"] == 1
    assert mock["unused_binding_count"] == 0
    assert mock["sessions_checked"] is True
    assert mock["plan"]["mode"] == "embedded"
    assert mock["plan"]["route_count"] == 1
    assert mock["plan"]["injection_count"] == 1
    assert report["run"]["mock"]["plan"] == str(plan_path.resolve())

    run_dir = project / ".astsight" / "runs" / report["run"]["run_id"]
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    document = json.loads(
        (run_dir / "mock_injection.json").read_text(encoding="utf-8")
    )
    assert manifest["token_fingerprint"]
    assert manifest["injections"][0]["injected"] == injected
    assert document["injections"][0]["property"] == "PAY_BASE_URL"
    assert document["injections"][0]["injected"] == injected
    assert '"token"' not in (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    assert "## 下游 Mock 计划" in (run_dir / "report.md").read_text(encoding="utf-8")

    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{plan_port}/_admin/health", timeout=1.0)


def test_mock_plan_conflicts_with_external_mock_flags(tmp_path: Path) -> None:
    project = tmp_path / "sut"
    project.mkdir()
    (project / "app.py").write_text(SUT_APP, encoding="utf-8")
    _make_store(project, endpoint="POST /pay", mock_bindings={DEP_ID: "success"})
    plan_path = _write_plan(project, _plan_payload())

    with pytest.raises(ValueError, match="不能与"):
        run(
            RunConfig(
                project=str(project),
                mode="cases",
                base_url="http://127.0.0.1:1",
                mock_plan=str(plan_path),
                mock_admin_url="http://127.0.0.1:2",
            )
        )
