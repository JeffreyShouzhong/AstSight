"""C3 下游 HTTP 调用点识别（P1）：AST 扫描、dep_id 合并与计划草案。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cli import main
from astsight.mock import load_mock_plan
from astsight.profile import DownstreamScanner, build_profiles, build_plan_from_profiles
from codegraph.client import RouteRef, SymbolRef
from codegraph.memory import InMemoryCodeGraphClient


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_literal_requests_and_httpx_calls_are_identified(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests
import httpx


def pay():
    requests.post("http://pay-service/gateway/pay", json={})
    httpx.get("http://stock-service/api/v1/status/")


def notify():
    httpx.post("http://notify-service/notify")
''',
    )
    scanner = DownstreamScanner(tmp_path)
    scan = scanner.scan("pay")

    assert scan.checked is True
    assert {dep["dep_id"] for dep in scan.deps} == {
        "http:host:pay-service POST /gateway/pay",
        "http:host:stock-service GET /api/v1/status",
    }
    assert scan.unsupported == []
    paths = {dep["path_key"] for dep in scan.deps}
    assert paths == {"/gateway/pay", "/api/v1/status"}


def test_config_fstring_keeps_path_and_marks_inferred(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import os

import httpx


def pay():
    base = os.environ["PAY_BASE_URL"]
    return httpx.get(f"{base}/gateway/pay", timeout=5)
''',
    )
    scan = DownstreamScanner(tmp_path).scan("pay")

    assert len(scan.deps) == 1
    dep = scan.deps[0]
    assert dep["dep_id"].startswith("http:call:")
    assert dep["dep_id"].endswith("GET /gateway/pay")
    assert dep["inferred"] is True
    assert dep["config_keys"] == ["PAY_BASE_URL"]
    assert scan.unsupported == []


def test_multiple_call_sites_merge_but_different_hosts_do_not(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests


def one():
    requests.get("http://pay-service/status")


def two():
    requests.get("http://pay-service/status/")
    requests.get("http://pay-service/status?trace=1")
    requests.get("http://stock-service/status")
''',
    )
    scanner = DownstreamScanner(tmp_path)
    one = scanner.scan("one")
    two = scanner.scan("two")

    assert [dep["dep_id"] for dep in one.deps] == ["http:host:pay-service GET /status"]
    by_id = {dep["dep_id"]: dep for dep in two.deps}
    assert set(by_id) == {
        "http:host:pay-service GET /status",
        "http:host:stock-service GET /status",
    }
    merged = by_id["http:host:pay-service GET /status"]
    assert len(merged["call_sites"]) == 2


def test_dynamic_url_and_https_are_unsupported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests


def run(payload):
    requests.get(payload["url"])
    requests.post("https://secure.example/pay")
    requests.get("http://ok.example/x")
''',
    )
    scan = DownstreamScanner(tmp_path).scan("run")

    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:ok.example GET /x"
    ]
    reasons = {item["reason"] for item in scan.unsupported}
    assert reasons == {"target_unresolved", "tls_not_supported"}
    dynamic = next(
        item for item in scan.unsupported if item["reason"] == "target_unresolved"
    )
    assert dynamic["file"] == "app.py"
    assert dynamic["line"] > 0


def test_transitive_local_callee_is_scanned(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests


def charge():
    requests.post("http://pay-service/gateway/pay")


def handler():
    charge()
''',
    )
    scan = DownstreamScanner(tmp_path).scan("handler")

    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:pay-service POST /gateway/pay"
    ]
    assert [site.handler for site in scan.call_sites] == ["charge"]


def test_codegraph_callees_expand_to_other_modules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
def handler():
    return fetch_balance()
''',
    )
    _write(
        tmp_path,
        "clients.py",
        '''
import httpx


def fetch_balance():
    return httpx.get("http://stock-service/api/v1/balance")
''',
    )
    graph = InMemoryCodeGraphClient(call_edges={"handler": ["fetch_balance"]})
    scanner = DownstreamScanner(tmp_path, codegraph_client=graph)
    scan = scanner.scan("handler")

    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:stock-service GET /api/v1/balance"
    ]


def test_request_method_session_and_urlopen(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import urllib.request

import httpx
import requests


def run():
    with requests.Session() as session:
        session.request("patch", "http://order-service/orders/1")
        session.put(url="http://order-service/orders/1")
    client = httpx.Client()
    client.request(url="http://order-service/orders/2", method="DELETE")
    urllib.request.urlopen("http://legacy-service/health")
''',
    )
    scan = DownstreamScanner(tmp_path).scan("run")

    assert {dep["dep_id"] for dep in scan.deps} == {
        "http:host:order-service PATCH /orders/1",
        "http:host:order-service PUT /orders/1",
        "http:host:order-service DELETE /orders/2",
        "http:host:legacy-service GET /health",
    }


def test_build_profiles_replaces_placeholder_deps(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests


def pay():
    requests.post("http://pay-service/gateway/pay")
''',
    )
    profiles, unmapped = build_profiles(
        [
            {
                "method": "POST",
                "path": "/pay",
                "handler": "pay",
                "file": "app.py",
                "source": "framework_plugin",
                "confidence": "verified",
            }
        ],
        [],
        project_root=tmp_path,
    )

    # 整个项目没有契约时，不按接口逐条刷 unmapped；缺口由 context 统一告警。
    assert unmapped == []
    profile = profiles[0]
    assert profile["contract_incomplete"] is True
    assert profile["contract_incomplete_reason"] == "openapi_missing"
    assert profile["downstream_checked"] is True
    assert profile["downstream_unmapped"] is False
    assert profile["downstream_deps"][0]["dep_id"] == (
        "http:host:pay-service POST /gateway/pay"
    )
    assert profile["downstream_call_sites"][0]["file"] == "app.py"


def test_plan_draft_is_valid_and_marks_unknown_injection_original(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import os

import requests


def pay():
    base = os.environ["PAY_BASE_URL"]
    requests.post(f"{base}/gateway/pay")
    requests.get("http://stock-service/api/v1/status")
''',
    )
    scanner = DownstreamScanner(tmp_path)
    scan = scanner.scan("pay")
    profiles = [{"downstream_deps": scan.deps}]
    payload = build_plan_from_profiles(profiles, sut_id="pay-sut")

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    plan = load_mock_plan(plan_path)

    by_dep = {route.dep_id: route for route in plan.routes}
    assert len(by_dep) == 2
    assert all(route.branches == {} for route in plan.routes)
    injections = {item.dep_id: item for item in plan.injections}
    cfg_dep = next(dep for dep in by_dep if dep.startswith("http:call:"))
    assert injections[cfg_dep].strategy.value == "unsupported"
    assert injections[cfg_dep].property == "PAY_BASE_URL"
    assert injections[cfg_dep].reason == "injection_original_unknown"
    host_dep = "http:host:stock-service GET /api/v1/status"
    assert injections[host_dep].strategy.value == "http_proxy"


def test_class_method_owner_module_constant_and_path_placeholder(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        '''
import requests

BASE = "http://order-service"


class PayView:
    def post(self, order_id):
        return requests.get(BASE + f"/orders/{order_id}")
''',
    )
    scan = DownstreamScanner(tmp_path).scan("post", owner="PayView")

    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:order-service GET /orders/{}"
    ]
    assert scan.call_sites[0].handler == "PayView.post"


def test_non_python_graph_is_scanned_for_supported_languages(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App.java",
        """
class App {
    String call() {
        return client.getForObject("http://pay-service/status", String.class);
    }
}
""",
    )
    graph = InMemoryCodeGraphClient(languages=["java"])
    scan = DownstreamScanner(tmp_path, codegraph_client=graph).scan("call")

    assert scan.checked is True
    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:pay-service GET /status"
    ]


def test_unsupported_language_graph_is_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "lib.rs", "fn handler() {}\n")
    graph = InMemoryCodeGraphClient(languages=["rust"])
    scan = DownstreamScanner(tmp_path, codegraph_client=graph).scan("handler")

    assert scan.checked is False
    assert scan.deps == []
    assert "downstream_language_not_supported:rust" in scan.warnings


def test_handler_without_source_is_not_guessed(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def other():\n    return 1\n")
    scan = DownstreamScanner(tmp_path).scan("missing_handler")

    assert scan.checked is False
    assert scan.deps == []
    assert "handler_source_missing:missing_handler" in scan.warnings


FASTAPI_APP = '''
import requests
from fastapi import FastAPI

app = FastAPI()


@app.get("/pay")
def pay():
    return requests.get("http://pay-service/gateway/pay").json()
'''


def test_mock_plan_cli_writes_valid_plan_and_refuses_overwrite(
    tmp_path: Path, capsys
) -> None:
    project = tmp_path / "sut"
    project.mkdir()
    _write(project, "app.py", FASTAPI_APP)

    code = main(
        [
            "mock",
            "plan",
            "--project",
            str(project),
            "--codegraph",
            "off",
            "--json",
        ]
    )
    assert code == 0, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["routes"] == 1
    assert payload["downstream_call_sites"] == 1

    plan_path = project / ".astsight" / "mock-plan.json"
    plan = load_mock_plan(plan_path)
    assert plan.sut_id == "sut"
    assert [route.dep_id for route in plan.routes] == [
        "http:host:pay-service GET /gateway/pay"
    ]
    assert plan.routes[0].branches == {}
    assert plan.injections[0].strategy.value == "http_proxy"

    code = main(
        ["mock", "plan", "--project", str(project), "--codegraph", "off", "--json"]
    )
    assert code == 1
    assert "拒绝覆盖" in capsys.readouterr().err

    code = main(
        [
            "mock",
            "plan",
            "--project",
            str(project),
            "--codegraph",
            "off",
            "--force",
            "--json",
        ]
    )
    assert code == 0
