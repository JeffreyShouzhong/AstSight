"""M1 确定性闭环的单元测试（不启动服务、不依赖 CodeGraph）。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from astsight.cases import generate_cases
from astsight.changes.git_diff import FileChange, diff_files
from astsight.changes.impact import analyze_impact
from astsight.changes.symbol_diff import ChangedSymbol, symbols_for_changes
from astsight.codegen import generate_pytest
from astsight.execute.diagnose import classify
from astsight.execute.runner import TestRecord as Record, _parse_junit, collect_only
from astsight.inventory import (
    _merge_openapi_routes,
    route_dicts,
    route_inventory_health,
)
from astsight.profile import load_spec, parse_operations, sample_value
from astsight.report import build_report
from codegraph.client import ImpactResult, RouteRef, SymbolRef
from codegraph.embedded import _split_route_name
from codegraph.memory import InMemoryCodeGraphClient
from codegraph.route_repair import (
    RouteDeclaration,
    RoutePrefixRepair,
    normalize_django_path,
)
from contracts import Assertion, AssertStrength, TestCase as Case

FASTAPI_ROUTER = '''
from fastapi import APIRouter, FastAPI

router = APIRouter(prefix="/items")


@router.get("/{item_id}")
def get_item(item_id: int):
    return {"item_id": item_id}


app = FastAPI()
app.include_router(router, prefix="/api/v1")
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_git_diff_reports_added_function_lines(tmp_path: Path) -> None:
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    (repo / "app.py").write_text(
        "def handler():\n    return 1\n\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature")

    (repo / "app.py").write_text(
        "def handler():\n    value = 1\n    return value\n\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change")

    changes, meta = diff_files(repo, "main", "feature")
    assert meta["comparison"] == "three_dot"
    assert [c.path for c in changes] == ["app.py"]
    # 只改了 handler 内部两行：区间不应覆盖 untouched
    covered = {line for start, end in changes[0].added_ranges for line in range(start, end + 1)}
    assert covered == {2, 3}


def test_fastapi_router_prefix_is_rebuilt(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(FASTAPI_ROUTER, encoding="utf-8")
    routes = RoutePrefixRepair(tmp_path).scan()
    endpoints = {(r.method, r.path) for r in routes}
    assert ("GET", "/api/v1/items/{item_id}") in endpoints


def test_openapi_operations_and_minimum_sample() -> None:
    spec = load_spec(_write_spec())
    operations = parse_operations(spec)
    assert operations[0].method == "POST"
    assert operations[0].body_required == ["quantity"]
    assert operations[0].success_codes == [201]
    assert sample_value({"type": "integer", "minimum": 3}, {}) == 3


def _write_spec() -> str:
    import tempfile

    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["quantity"],
                                    "properties": {
                                        "quantity": {"type": "integer", "minimum": 1, "maximum": 9}
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"order_id": {"type": "integer"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(spec, handle)
    handle.close()
    return handle.name


def test_case_templates_cover_positive_and_negative() -> None:
    profile = {
        "endpoint": "POST /orders",
        "method": "POST",
        "path": "/orders",
        "contract_incomplete": False,
        "params": [],
        "request_body": {
            "type": "object",
            "required": ["quantity"],
            "properties": {
                "quantity": {"type": "integer", "minimum": 1, "maximum": 9},
                "status": {"type": "string", "enum": ["pending", "paid"]},
            },
        },
        "body_required": ["quantity"],
        "success_codes": [201],
        "response_fields": {"201": ["order_id"]},
        "source": "openapi",
    }
    cases = generate_cases([profile])
    categories = [c.category for c in cases]
    assert categories[0] == "positive"
    assert "missing_required" in categories
    assert "type_error" in categories
    assert "boundary" in categories
    assert all(c.assert_strength == AssertStrength.L1 for c in cases)
    assert cases[0].asserts[0].kind == "status_code"


def test_generated_code_can_be_collected_without_running(tmp_path: Path) -> None:
    case = Case(
        id="TC-0001",
        endpoint="GET /health",
        category="positive",
        method="GET",
        path="/health",
        asserts=[Assertion(kind="status_code", op="in", expect=[200])],
        source="test",
    )
    result = generate_pytest([case], tmp_path / "generated")
    assert not result.errors
    ok, output = collect_only(tmp_path / "generated", timeout=60)
    assert ok, output


def test_diagnose_500_is_sut_defect_and_connection_is_retryable() -> None:
    case = Case(
        id="TC-0001", endpoint="GET /x", category="positive", method="GET",
        path="/x", source="test",
    )
    bug = Record(
        nodeid="t.py::T::test", case_id="TC-0001", classname="t.T", name="test",
        outcome="failed", message="expected status in [200], got 500: boom",
    )
    assert classify(bug, case)["root_cause"] == "code_defect"
    assert classify(bug, case)["retryable"] is False

    flake = Record(
        nodeid="t.py::T::test", case_id="TC-0001", classname="t.T", name="test",
        outcome="failed", message="httpx.ConnectError: [Errno 111] Connection refused",
    )
    assert classify(flake, case)["reason"] == "env_flake"
    assert classify(flake, case)["retryable"] is True


def test_impact_direct_handler_hit_is_verified() -> None:
    symbol = ChangedSymbol(
        name="get_order", qualified_name="app.get_order", kind="function",
        file="app.py", side="new", lines=[10],
    )
    routes = [
        {"method": "GET", "path": "/orders/{id}", "handler": "get_order",
         "file": "app.py", "source": "framework_plugin", "confidence": "verified"},
    ]
    summary = analyze_impact([symbol], routes, codegraph_client=None)
    assert len(summary.changed_apis) == 1
    assert summary.changed_apis[0]["confidence"] == "high"
    assert summary.changed_apis[0]["evidence"] == "verified"
    assert not summary.unresolved


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")


def test_java_change_line_maps_to_codegraph_method(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    java = "src/main/java/com/example/OrderController.java"
    client = InMemoryCodeGraphClient(
        symbols_by_file={
            java: [
                SymbolRef(name="OrderController", kind="class", file=java,
                          line=10, end_line=80, node_id="class-1", language="java"),
                SymbolRef(name="getOrder", kind="method", file=java,
                          line=20, end_line=30, node_id="method-get", language="java"),
                SymbolRef(name="createOrder", kind="method", file=java,
                          line=40, end_line=60, node_id="method-create", language="java"),
            ]
        },
        languages=["java"],
    )
    changes = [FileChange(path=java, added_ranges=[(22, 22)])]

    symbols, unsupported = symbols_for_changes(
        tmp_path, "main", "feature", changes, codegraph_client=client
    )

    assert not unsupported
    assert len(symbols) == 1
    symbol = symbols[0]
    assert symbol.name == "getOrder"
    assert symbol.kind == "method"
    assert symbol.node_id == "method-get"
    assert symbol.evidence == "verified"
    assert symbol.lines == [22]


def test_java_class_level_line_expands_to_members_as_inferred(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    java = "src/main/java/com/example/OrderController.java"
    client = InMemoryCodeGraphClient(
        symbols_by_file={
            java: [
                SymbolRef(name="OrderController", kind="class", file=java,
                          line=10, end_line=80, node_id="class-1", language="java"),
                SymbolRef(name="getOrder", kind="method", file=java,
                          line=20, end_line=30, node_id="method-get", language="java"),
                SymbolRef(name="createOrder", kind="method", file=java,
                          line=40, end_line=60, node_id="method-create", language="java"),
            ]
        },
        languages=["java"],
    )
    changes = [FileChange(path=java, added_ranges=[(11, 11)])]

    symbols, unsupported = symbols_for_changes(
        tmp_path, "main", "feature", changes, codegraph_client=client
    )

    assert not unsupported
    assert {symbol.name for symbol in symbols} == {"getOrder", "createOrder"}
    assert all(symbol.evidence == "inferred" for symbol in symbols)


def test_java_pure_deletion_falls_back_to_file_level(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    java = "src/main/java/com/example/OrderController.java"
    client = InMemoryCodeGraphClient(
        symbols_by_file={
            java: [
                SymbolRef(name="OrderController", kind="class", file=java,
                          line=10, end_line=80, node_id="class-1", language="java"),
                SymbolRef(name="getOrder", kind="method", file=java,
                          line=20, end_line=30, node_id="method-get", language="java"),
            ]
        },
        languages=["java"],
    )
    changes = [FileChange(path=java, removed_ranges=[(25, 26)])]

    symbols, unsupported = symbols_for_changes(
        tmp_path, "main", "feature", changes, codegraph_client=client
    )

    assert not unsupported
    assert len(symbols) == 1
    assert symbols[0].kind == "file"
    assert symbols[0].side == "old"
    assert symbols[0].evidence == "inferred"


def test_non_python_without_codegraph_is_explicitly_unsupported(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    changes = [
        FileChange(path="src/main/java/com/example/OrderController.java",
                   added_ranges=[(22, 22)])
    ]

    symbols, unsupported = symbols_for_changes(
        tmp_path, "main", "feature", changes, codegraph_client=None
    )

    assert symbols == []
    assert unsupported[0]["reason"].startswith("language_not_supported")


class _RecordingImpactClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def impact(self, name: str, depth: int = 3, *, node_id: str = "") -> ImpactResult:
        self.calls.append((name, node_id))
        return ImpactResult(
            symbol=name,
            affected=["helper", "handle"],
            affected_routes=[
                RouteRef(method="GET", path="/target", handler="handle")
            ],
            trace=["helper", "handle"],
        )


def test_impact_uses_node_id_and_disambiguates_same_name_by_file() -> None:
    symbol = ChangedSymbol(
        name="helper", qualified_name="com.example.Helper.helper", kind="method",
        file="src/main/java/com/example/Helper.java", node_id="node-helper",
    )
    routes = [
        # 同名 handler 在另一个 Controller：不能直接算受影响，必须走图谱
        {"method": "GET", "path": "/other", "handler": "helper",
         "file": "src/main/java/com/example/OtherController.java"},
    ]
    client = _RecordingImpactClient()

    summary = analyze_impact([symbol], routes, codegraph_client=client)

    assert client.calls == [("helper", "node-helper")]
    assert [api["path"] for api in summary.changed_apis] == ["/target"]
    assert summary.changed_apis[0]["evidence"] == "inferred"


def test_file_level_change_hits_routes_in_same_file_without_graph() -> None:
    symbol = ChangedSymbol(
        name="OrderController",
        qualified_name="src/main/java/com/example/OrderController.java",
        kind="file",
        file="src/main/java/com/example/OrderController.java",
        evidence="inferred",
    )
    routes = [
        {"method": "GET", "path": "/orders", "handler": "getOrder",
         "file": "src/main/java/com/example/OrderController.java"},
        {"method": "GET", "path": "/other", "handler": "other",
         "file": "src/main/java/com/example/OtherController.java"},
    ]

    summary = analyze_impact([symbol], routes, codegraph_client=None)

    assert [api["path"] for api in summary.changed_apis] == ["/orders"]
    assert summary.changed_apis[0]["evidence"] == "inferred"
    assert summary.changed_apis[0]["confidence"] == "medium"


class _EmptyImpactClient:
    def impact(self, name: str, depth: int = 3, *, node_id: str = "") -> ImpactResult:
        return ImpactResult(symbol=name)


def _write_controller(tmp_path: Path, name: str, annotation: str = "@RestController") -> str:
    rel = f"src/main/java/com/example/{name}.java"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"package com.example;\n{annotation}\npublic class {name} {{}}\n"
    path.write_text(body, encoding="utf-8")
    return rel


def _openapi_route(method: str, path: str, operation_id: str) -> dict:
    return {
        "method": method,
        "path": path,
        "handler": "",
        "file": "",
        "source": "openapi",
        "confidence": "verified",
        "operation_id": operation_id,
    }


def test_operation_id_fallback_attributes_low_inferred(tmp_path: Path) -> None:
    rel = _write_controller(tmp_path, "OwnerRestControllerV1")
    symbol = ChangedSymbol(
        name="listOwners",
        qualified_name="com.example.OwnerRestControllerV1.listOwners",
        kind="method",
        file=rel,
    )
    summary = analyze_impact(
        [symbol],
        [_openapi_route("GET", "/owners", "listOwners")],
        codegraph_client=_EmptyImpactClient(),
        project_root=tmp_path,
    )

    assert [api["handler"] for api in summary.changed_apis] == ["GET /owners"]
    api = summary.changed_apis[0]
    assert api["confidence"] == "low"
    assert api["evidence"] == "inferred"
    assert api["attribution"] == "openapi_operation_id"
    assert "operationId:listOwners" in api["trace"]
    assert summary.unresolved == []


def test_operation_id_fallback_works_without_codegraph(tmp_path: Path) -> None:
    rel = _write_controller(tmp_path, "OwnerRestControllerV1")
    symbol = ChangedSymbol(
        name="listOwners",
        qualified_name="com.example.OwnerRestControllerV1.listOwners",
        kind="method",
        file=rel,
    )

    summary = analyze_impact(
        [symbol],
        [_openapi_route("GET", "/owners", "listOwners")],
        codegraph_client=None,
        project_root=tmp_path,
    )

    assert [api["path"] for api in summary.changed_apis] == ["/owners"]
    assert summary.changed_apis[0]["confidence"] == "low"


def test_operation_id_fallback_skips_ambiguous_match(tmp_path: Path) -> None:
    rel = _write_controller(tmp_path, "OwnerRestControllerV1")
    symbol = ChangedSymbol(
        name="listOwners",
        qualified_name="com.example.OwnerRestControllerV1.listOwners",
        kind="method",
        file=rel,
    )
    routes = [
        _openapi_route("GET", "/owners", "listOwners"),
        _openapi_route("GET", "/v2/owners", "listOwners"),
    ]

    summary = analyze_impact([symbol], routes, codegraph_client=None, project_root=tmp_path)

    assert summary.changed_apis == []
    assert summary.unresolved[0]["reason"] == "ambiguous_operation_id"


def test_operation_id_fallback_requires_controller_file(tmp_path: Path) -> None:
    rel = _write_controller(tmp_path, "OwnerService", annotation="")
    symbol = ChangedSymbol(
        name="listOwners",
        qualified_name="com.example.OwnerService.listOwners",
        kind="method",
        file=rel,
    )

    summary = analyze_impact(
        [symbol],
        [_openapi_route("GET", "/owners", "listOwners")],
        codegraph_client=None,
        project_root=tmp_path,
    )

    assert summary.changed_apis == []
    assert summary.unresolved[0]["reason"] == "no_codegraph"


def test_operation_id_fallback_ignores_routes_with_handler(tmp_path: Path) -> None:
    rel = _write_controller(tmp_path, "OwnerRestControllerV1")
    symbol = ChangedSymbol(
        name="listOwners",
        qualified_name="com.example.OwnerRestControllerV1.listOwners",
        kind="method",
        file=rel,
    )
    route = _openapi_route("GET", "/owners", "listOwners")
    route["handler"] = "someoneElse"

    summary = analyze_impact([symbol], [route], codegraph_client=None, project_root=tmp_path)

    assert summary.changed_apis == []
    assert summary.unresolved[0]["reason"] == "no_codegraph"


def test_junit_nodeid_is_pytest_selectable(tmp_path: Path) -> None:
    xml = tmp_path / "results.xml"
    xml.write_text(
        '<testsuite><testcase classname="test_orders.TestOrders" '
        'name="test_tc_0007_positive" time="0.01" /></testsuite>',
        encoding="utf-8",
    )
    records = _parse_junit(xml)
    assert records[0].case_id == "TC-0007"
    # 重试要用 pytest 能识别的 nodeid，不能是 "test_orders.TestOrders::..."
    assert records[0].nodeid == "test_orders.py::TestOrders::test_tc_0007_positive"


def test_report_gate_blocks_on_code_defect() -> None:
    report = build_report(
        run_meta={"run_id": "r", "mode": "delta"},
        changes={},
        impact={"changed_apis": [], "unresolved_symbols": []},
        profiles=[],
        cases=[],
        records=[{
            "case_id": "TC-0001",
            "endpoint": "GET /x",
            "category": "positive",
            "outcome": "failed",
            "attempts": [{"n": 1, "status": "fail"}],
            "diagnosis": {"reason": "sut_bug", "root_cause": "code_defect",
                          "action_target": "user", "retryable": False},
        }],
        unmapped={"symbols": [], "routes": [], "unsupported_files": []},
        route_audit={},
        codegen={},
    )
    assert report["summary"]["sut_defects"] == 1
    assert report["gate"]["verdict"] == "fail"
    assert any("真实缺陷" in blocker for blocker in report["gate"]["blockers"])


def test_route_inventory_health_counts_controllers(tmp_path: Path) -> None:
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    (package / "OwnerController.java").write_text(
        "@RestController\nclass OwnerController {}", encoding="utf-8"
    )
    (package / "PetController.java").write_text(
        "@Controller\nclass PetController {}", encoding="utf-8"
    )
    # Advice 不是路由入口，不能被算成 Controller
    (package / "Errors.java").write_text(
        "@RestControllerAdvice\nclass Errors {}", encoding="utf-8"
    )
    # test 目录不在扫描范围内
    test_dir = tmp_path / "src" / "test" / "java"
    test_dir.mkdir(parents=True)
    (test_dir / "FakeController.java").write_text(
        "@RestController\nclass FakeController {}", encoding="utf-8"
    )

    routes = [{"handler": f"h{i}", "method": "GET", "path": f"/p{i}"} for i in range(4)]
    health = route_inventory_health(tmp_path, routes, openapi_operations=4)

    assert health["controller_files"] == 2
    assert health["attributed_routes"] == 4
    assert health["status"] == "ok"


def test_route_inventory_health_flags_generated_interface_style(tmp_path: Path) -> None:
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    for name in ("Owner", "Pet", "Vet"):
        (package / f"{name}Controller.java").write_text(
            "@RestController\nclass X {}", encoding="utf-8"
        )

    # 真实场景：注解在生成接口里，源码只提取到一个类级路径
    routes = [{"handler": "redirect", "method": "", "path": "/"}]
    health = route_inventory_health(tmp_path, routes, openapi_operations=37)

    assert health["status"] == "degraded"
    reasons = " ".join(health["reasons"])
    assert "attributed_routes_below_openapi:1/37" in reasons
    assert "attributed_routes_below_controller_files:1/3" in reasons


def test_openapi_routes_supplement_inventory_without_handler(tmp_path: Path) -> None:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "demo", "version": "1"},
        "paths": {
            "/owners": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/pets": {"post": {"responses": {"200": {"description": "ok"}}}},
        },
    }
    source = tmp_path / "openapi.json"
    source.write_text(json.dumps(spec), encoding="utf-8")

    routes, report = _merge_openapi_routes(
        [{"method": "GET", "path": "/owners", "handler": "list_owners"}],
        str(source),
    )

    assert report == {"added": 1, "operations": 2, "source": str(source)}
    assert [(route["method"], route["path"]) for route in routes] == [
        ("GET", "/owners"),
        ("POST", "/pets"),
    ]
    # 补入项没有 handler/file：可用于清单与全量画像，但不能参与变更归因
    assert routes[1]["handler"] == ""
    assert routes[1]["file"] == ""
    assert routes[1]["source"] == "openapi"


def test_openapi_routes_skip_remote_source() -> None:
    routes, report = _merge_openapi_routes(
        [], "https://example.test/openapi.json"
    )
    assert routes == []
    assert report == {"added": 0, "skipped": "remote_source"}


def test_report_warns_when_route_inventory_degraded() -> None:
    report = build_report(
        run_meta={"run_id": "r", "mode": "delta"},
        changes={},
        impact={"changed_apis": [], "unresolved_symbols": []},
        profiles=[],
        cases=[],
        records=[],
        unmapped={"symbols": [], "routes": [], "unsupported_files": []},
        route_audit={
            "health": {
                "status": "degraded",
                "reasons": ["attributed_routes_below_openapi:1/37"],
            }
        },
        codegen={},
    )
    assert any(
        "route_inventory_degraded" in warning
        for warning in report["gate"]["warnings"]
    )
    # 这是“可能少报”的告警，不是阻塞
    assert report["gate"]["verdict"] != "fail"


def test_route_health_flags_duplicates_and_missing_methods(tmp_path: Path) -> None:
    routes = [
        {"method": "GET", "path": "/orders", "handler": "list", "handler_owner": "A"},
        {"method": "GET", "path": "/orders", "handler": "list", "handler_owner": "B"},
        {"method": "", "path": "/legacy", "handler": "legacy"},
        {"method": "VIEWSET", "path": "/articles", "handler": "ArticleViewSet"},
    ]
    health = route_inventory_health(tmp_path, routes)

    assert health["status"] == "degraded"
    assert health["duplicate_endpoints"] == 1
    assert health["routes_without_http_method"] == 1
    assert health["routes_with_unknown_http_method"] == 1
    reasons = " ".join(health["reasons"])
    assert "duplicate_route_endpoints:1" in reasons
    assert "routes_without_http_method:1" in reasons
    assert "routes_with_unknown_http_method:1" in reasons


def test_split_route_name_drops_viewset_and_normalizes_django_regex() -> None:
    assert _split_route_name("VIEWSET /articles") == ("", "/articles")
    assert _split_route_name("^articles/feed/?$") == ("", "/articles/feed")
    assert _split_route_name(
        "GET /articles/(?P<slug>[-a-zA-Z0-9_]+)/$"
    ) == ("GET", "/articles/{slug}/")
    assert normalize_django_path(r"^items/(?P<item_id>\d+)/$") == "/items/{item_id}/"


def test_fastapi_submodule_router_and_settings_prefix(tmp_path: Path) -> None:
    (tmp_path / "app" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "api" / "routes" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "settings.py").write_text(
        'class Settings:\n    api_prefix = "/api"\n',
        encoding="utf-8",
    )
    (tmp_path / "app" / "api" / "routes" / "items.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/items')\n"
        "@router.get('/{item_id}')\n"
        "def get_item(item_id: int):\n"
        "    return {'item_id': item_id}\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "api" / "router.py").write_text(
        "from fastapi import APIRouter\n"
        "from app.api.routes import items\n"
        "router = APIRouter()\n"
        "router.include_router(items.router, prefix='/v1')\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from settings import Settings\n"
        "from app.api.router import router as api_router\n"
        "settings = Settings()\n"
        "app = FastAPI()\n"
        "app.include_router(api_router, prefix=settings.api_prefix)\n",
        encoding="utf-8",
    )

    routes = RoutePrefixRepair(tmp_path).scan()
    endpoints = {(route.method, route.path) for route in routes}

    assert ("GET", "/api/v1/items/{item_id}") in endpoints
    route = next(r for r in routes if r.path == "/api/v1/items/{item_id}")
    # 前缀来自 settings 字面量，是推导而非显式声明
    assert route.inferred is True
    assert route.note == "include_prefix_inferred"


def test_django_url_as_view_and_drf_expansion(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "shop" / "views.py").write_text(
        "from rest_framework.views import APIView\n"
        "from rest_framework.viewsets import ModelViewSet\n"
        "\n"
        "class ArticleViewSet(ModelViewSet):\n"
        "    lookup_field = 'slug'\n"
        "    def list(self, request):\n"
        "        return []\n"
        "    def create(self, request):\n"
        "        return {}\n"
        "    def retrieve(self, request, slug=None):\n"
        "        return {}\n"
        "\n"
        "class ItemAPIView(APIView):\n"
        "    def get(self, request, item_id):\n"
        "        return {}\n"
        "    def post(self, request, item_id):\n"
        "        return {}\n"
        "\n"
        "def feed(request):\n"
        "    return {}\n",
        encoding="utf-8",
    )
    (tmp_path / "shop" / "urls.py").write_text(
        "from django.urls import include, path, url\n"
        "from rest_framework.routers import DefaultRouter\n"
        "from . import views\n"
        "\n"
        "router = DefaultRouter()\n"
        "router.register(r'articles', views.ArticleViewSet)\n"
        "\n"
        "urlpatterns = [\n"
        "    url(r'^items/(?P<item_id>\\d+)/$', views.ItemAPIView.as_view()),\n"
        "    url(r'^feed/?$', views.feed),\n"
        "    path('', include(router.urls)),\n"
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "urls.py").write_text(
        "from django.urls import include, path\n"
        "urlpatterns = [path('api/', include('shop.urls'))]\n",
        encoding="utf-8",
    )

    routes = RoutePrefixRepair(tmp_path).scan()
    endpoints = {(route.method, route.path) for route in routes}

    # Django 函数视图的 method 在 view 内部按 request.method 分支，静态不可知：
    # 如实保留空 method，由健康检查告警，不猜 GET。
    assert ("", "/api/feed") in endpoints
    assert ("GET", "/api/items/{item_id}/") in endpoints
    assert ("POST", "/api/items/{item_id}/") in endpoints
    assert ("GET", "/api/articles/") in endpoints
    assert ("POST", "/api/articles/") in endpoints
    assert ("GET", "/api/articles/{slug}/") in endpoints
    assert ("PUT", "/api/articles/{slug}/") in endpoints
    assert ("PATCH", "/api/articles/{slug}/") in endpoints
    assert ("DELETE", "/api/articles/{slug}/") in endpoints

    drf = [r for r in routes if r.handler_owner == "ArticleViewSet"]
    assert drf and all(r.inferred for r in drf)
    assert all(r.file == "shop/views.py" for r in drf)
    assert all(r.route_file == "shop/urls.py" for r in drf)


def test_direct_route_requires_class_owner_for_viewset_actions() -> None:
    routes = [
        {
            "method": "GET", "path": "/articles", "handler": "list",
            "handler_owner": "ArticleViewSet", "file": "shop/views.py",
        },
        {
            "method": "GET", "path": "/tags", "handler": "list",
            "handler_owner": "TagViewSet", "file": "shop/views.py",
        },
    ]
    symbol = ChangedSymbol(
        name="list",
        qualified_name="ArticleViewSet.list",
        kind="method",
        file="shop/views.py",
        lines=[1],
    )

    summary = analyze_impact([symbol], routes)

    assert [api["path"] for api in summary.changed_apis] == ["/articles"]
    assert not summary.unresolved


def test_symbol_diff_merges_new_and_old_sides(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text(
        "def handler():\n    return 1\n", encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "app.py").write_text(
        "def handler():\n    return 2\n", encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "change")

    changes, _ = diff_files(tmp_path, "main", "feature")
    symbols, unsupported = symbols_for_changes(
        tmp_path, "main", "feature", changes
    )

    assert not unsupported
    assert [(s.qualified_name, s.side) for s in symbols] == [("app.handler", "new")]


def test_route_dicts_does_not_call_inferred_routes_verified() -> None:
    declaration = RouteDeclaration(
        framework="django",
        method="GET",
        path="/api/articles/",
        handler="list",
        file="app/views.py",
        line=1,
        handler_owner="ArticleViewSet",
        inferred=True,
        note="drf_router_expanded",
    )

    route = route_dicts([declaration])[0]

    assert route["confidence"] == "inferred"
    assert route["handler_owner"] == "ArticleViewSet"
    assert route["route_file"] == "app/views.py"


def test_graph_root_path_matches_unique_handler_as_inferred(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import APIRouter, FastAPI\n"
        "router = APIRouter(prefix='/items')\n"
        "@router.get('/{item_id}')\n"
        "def get_item(item_id: int):\n"
        "    return {'item_id': item_id}\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/api')\n",
        encoding="utf-8",
    )
    graph = [
        RouteRef(method="GET", path="/", handler="get_item", file="app.py")
    ]

    routes = RoutePrefixRepair(tmp_path).repair(graph)

    assert len(routes) == 1
    assert routes[0].path == "/api/items/{item_id}"
    assert routes[0].confidence == "inferred"
    assert routes[0].note == "graph_path_missing"
