"""`agent run` 全链路（HTTP 入口 + Dubbo 下游）：画像 → 计划 → 注入 → 执行 → 报告。

被测样例 `java/samples/spring-boot-dubbo` 的 HTTP 入口 `/orders/{id}` 调用
`@DubboReference` 字段声明的 Dubbo 服务。模型响应脚本化，断言确定性边界：

- 画像从 Java 源码识别 Dubbo 依赖（接口、方法、group/version、参数签名）；
- 计划生成 `dubbo_routes`，注入策略 `agent_rewrite`，原因
  `dubbo_filter_requires_java_agent`；
- SUT 通过 `JAVA_TOOL_OPTIONS` + `token_env` 加载 agent，命令行与日志不含明文
  token；
- 请求经 Spring 入口进入 Dubbo 消费端代理，Filter 向 Mock 取桩并直接返回；
- 目标 Dubbo 端口零建连、零字节（真实调用 0 次）；
- 证据 `adapter=dubbo-filter` / `mode=agent_filter`，心跳上报 Filter 已实例化，
  报告门禁 pass（`filter_not_in_chain` 不阻断，`serialization_verified=false`
  只作为告警）。

启用：

- `ASTSIGHT_JAVA_AGENT_DUBBO_FLOW_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- 已构建样例：`cd java/samples/spring-boot-dubbo && mvn -q -DskipTests package`
- JDK 17+（样例字节码 release 17）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_DUBBO_FLOW_E2E") != "1",
    reason="Dubbo 全链路联调默认跳过：设置 ASTSIGHT_JAVA_AGENT_DUBBO_FLOW_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "spring-boot-dubbo"
DOC = "docs/requirements.md"
ENDPOINT = "GET /orders/{id}"
RULE_TEXT = "按订单号调用下游订单服务，返回订单号与金额"
DEP_ID = (
    "dubbo:io.astsight.sample.dubbo.OrderApi#create(java.lang.String)"
    "?group=order&version=1.0.0"
)


class _Usage:
    def as_dict(self) -> dict:
        return {"prompt_chars": 10, "attempts": 1}


class ScriptedLLM:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls: list[dict[str, str]] = []
        self.config = SimpleNamespace(
            redacted=lambda: {"provider": "fake", "model": "fake"}
        )
        self.usage = [_Usage()]

    def complete_json(self, *, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        return self.payloads.pop(0)


class CountingListener:
    """在 Dubbo 目标端口记录建连与收到的字节；收到字节说明真实 RPC 发生了。"""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = int(self._socket.getsockname()[1])
        self.accepts = 0
        self.bytes_received = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "CountingListener":
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                self.accepts += 1
                connection.settimeout(0.2)
                try:
                    payload = connection.recv(4096)
                    self.bytes_received += len(payload)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._socket.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _java() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", "").strip()
    if configured:
        candidate = Path(configured)
        candidates.append(
            candidate / "bin" / "java" if candidate.is_dir() else candidate
        )
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        candidates.append(Path(home) / "bin" / "java")
    found = shutil.which("java")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
        exe = Path(str(candidate) + ".exe")
        if exe.is_file():
            return exe
    pytest.skip("未找到 java；设置 ASTSIGHT_JAVA_AGENT_E2E_JAVA 或 JAVA_HOME")


def _agent_jar() -> Path:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests package")
    return agent


def _sample_jar() -> Path:
    jars = [
        path
        for path in (SAMPLE / "target").glob("spring-boot-dubbo-sample-*.jar")
        if not path.name.endswith(".original")
    ]
    if not jars:
        pytest.skip(
            "Dubbo 全链路样例未构建：cd java/samples/spring-boot-dubbo "
            "&& mvn -q -DskipTests package"
        )
    return sorted(jars)[0]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "java_dubbo_flow"
    shutil.copytree(SAMPLE / "src", project / "src")
    shutil.copy2(SAMPLE / "pom.xml", project / "pom.xml")
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "requirements.md").write_text(
        "# 订单服务\n\n"
        f"## {ENDPOINT}\n\n"
        f"{RULE_TEXT}。\n",
        encoding="utf-8",
    )
    return project


def _context(project: Path) -> dict:
    routes, _audit, _graph, _notes = inventory_routes(project, codegraph="off")
    return build_context(project, [DOC], routes=routes)


def _section(context: dict) -> str:
    for document in context["documents"]:
        for section in document["sections"]:
            if section["section"].endswith(ENDPOINT):
                return section["section"]
    raise AssertionError(f"section not found: {ENDPOINT}")


def _dep_id(context: dict) -> str:
    for profile in context["profiles"]:
        if profile["endpoint"] != ENDPOINT:
            continue
        for dep in profile.get("downstream_deps") or []:
            if str(dep.get("dep_type")) == "dubbo":
                return str(dep["dep_id"])
    raise AssertionError(f"dubbo dep not found: {ENDPOINT}")


def _rules(section: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "summary": "订单服务经 Dubbo 下游返回订单号与金额",
        "rules": [
            {
                "text": RULE_TEXT,
                "type": "other",
                "entities": ["订单"],
                "fields": [],
                "source": {
                    "doc": DOC,
                    "section": section,
                    "quote": RULE_TEXT,
                    "kind": "explicit",
                },
                "authority": "A",
                "impact": "P1-功能",
                "priority": "P1",
                "rule_id": "BR-01",
            }
        ],
        "questions": [],
        "gaps": [],
        "conflicts": [],
    }


def _mock_semantics(dep_id: str, key: str) -> dict:
    return {
        "deps": [
            {
                "dep_id": dep_id,
                "branches": [
                    {
                        "branch": "success",
                        "semantics": "下游创建订单成功并返回订单号与金额",
                        "stub": {"body": {"orderId": "O-1", "amount": 10}},
                        "evidence": "inferred",
                        "source": {"kind": "rule", "rule_keys": [key]},
                    }
                ],
            }
        ],
        "gaps": [],
        "questions": [],
    }


def _draft(dep_id: str, key: str, section: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "cases": [
            {
                "case_key": "dubbo_order_ok",
                "title": "HTTP 入口经 Dubbo Filter 取桩",
                "level": "business",
                "requirement_refs": [
                    {"doc": DOC, "section": section, "quote": RULE_TEXT}
                ],
                "rule_refs": [{"rule_key": key, "rule_id": "BR-01"}],
                "endpoint": ENDPOINT,
                "category": "positive",
                "path_params": {"id": "A-1"},
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200},
                    {"kind": "body_field", "target": "orderId", "op": "eq", "expect": "O-1"},
                    {"kind": "body_field", "target": "amount", "op": "eq", "expect": 10},
                ],
                "mock_bindings": {dep_id: "success"},
                "rationale": "Dubbo 下游返回桩，HTTP 入口原样返回订单号与金额",
            }
        ],
    }


def _config(
    project: Path, port: int, app_jar: Path, dubbo_port: int
) -> FlowConfig:
    return FlowConfig(
        project=str(project),
        requirements=[DOC],
        mode="full",
        codegraph="off",
        base_url=f"http://127.0.0.1:{port}",
        sut_command=[
            str(_java()),
            "-jar",
            str(app_jar),
            f"--server.port={port}",
            "--server.address=127.0.0.1",
            f"--dubbo.url=dubbo://127.0.0.1:{dubbo_port}?lazy=true",
        ],
        approve_as="e2e",
        out=".astsight/agent",
        timeout=240.0,
        sut_timeout=120.0,
        allow_unmapped=True,
    )


def test_agent_run_short_circuits_dubbo_downstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(_agent_jar()))
    project = _project(tmp_path)
    context = _context(project)
    section = _section(context)
    dep_id = _dep_id(context)
    assert dep_id == DEP_ID
    key = rule_key(DOC, section, RULE_TEXT)

    llm = ScriptedLLM(
        [
            _rules(section),
            _mock_semantics(dep_id, key),
            _draft(dep_id, key, section),
        ]
    )
    listener = CountingListener().start()
    try:
        result = run_flow(
            _config(project, _free_port(), _sample_jar(), listener.port), llm=llm
        )
    finally:
        listener.stop()

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    # 序列化保真未验证只降级为告警，不阻断；agent 注入必须成功。
    warnings = [str(item) for item in result.warnings]
    assert not [item for item in warnings if item.startswith("injection_agent_unavailable")], warnings
    gate = (result.report or {}).get("gate") or {}
    assert gate.get("verdict") == "pass", gate
    assert not [item for item in gate.get("blockers") or [] if "filter_not_in_chain" in item], gate
    assert any(
        "serialization_verified=false" in item for item in gate.get("warnings") or []
    ), gate
    mock_plan = ((result.report or {}).get("mock") or {}).get("plan") or {}
    assert mock_plan.get("dubbo_route_count") == 1, mock_plan
    run_dir = Path(result.run_dir)

    injection = json.loads((run_dir / "mock_injection.json").read_text("utf-8"))
    assert injection["dubbo_deps"] == [dep_id]
    strategies = {
        item["dep_id"]: (item["strategy"], item["reason"])
        for item in injection["injections"]
    }
    assert strategies == {
        dep_id: ("agent_rewrite", "dubbo_filter_requires_java_agent")
    }

    plan = json.loads((run_dir / "mock_plan.json").read_text("utf-8"))
    assert len(plan["dubbo_routes"]) == 1
    route = plan["dubbo_routes"][0]
    assert route["dep_id"] == dep_id
    assert route["service"] == "io.astsight.sample.dubbo.OrderApi"
    assert route["method"] == "create"
    assert route["parameter_types"] == ["java.lang.String"]
    assert route["group"] == "order"
    assert route["version"] == "1.0.0"
    assert sorted(route["branches"]) == ["success"]

    records = json.loads((run_dir / "execution.json").read_text("utf-8"))["records"]
    assert len(records) == 1
    assert records[0]["outcome"] == "passed", records

    calls = [
        json.loads(line)
        for line in (run_dir / "mock_calls.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    dubbo_calls = [call for call in calls if call.get("adapter") == "dubbo-filter"]
    assert len(dubbo_calls) == 1, calls
    call = dubbo_calls[0]
    assert call["mode"] == "agent_filter"
    assert call["dep_id"] == dep_id
    assert call["branch"] == "success"
    assert call["result"] == "ok"
    assert call["original_path"] == "dubbo:io.astsight.sample.dubbo.OrderApi#create"
    assert all(item.get("result") == "ok" for item in calls)

    # 消费端 Filter 短路：真实 Dubbo 端口零建连、零字节。
    assert listener.accepts == 0
    assert listener.bytes_received == 0

    evidence = json.loads((run_dir / "mock_evidence.json").read_text("utf-8"))
    heartbeat = (evidence.get("agent") or {}).get("heartbeat") or {}
    dubbo = heartbeat.get("dubbo") or {}
    assert dubbo.get("installed") is True, evidence.get("agent")
    assert dubbo.get("version") == "2.7.22"
    assert dubbo.get("filter_instantiated", 0) >= 1
    # 心跳默认 5s 一次，报告读到的可能是调用前那一拍；本次调用的凭据是
    # mock_calls.jsonl 里的证据行，不要求 filter_hits 已经回填。
    assert dubbo.get("serialization_verified") is False

    manifest = json.loads((run_dir / "run_manifest.json").read_text("utf-8"))
    assert manifest["token_fingerprint"]
    assert "token" not in manifest

    sut_log = (run_dir / "sut.log").read_text("utf-8", errors="replace")
    assert "Picked up JAVA_TOOL_OPTIONS" in sut_log
    assert "-javaagent:" in sut_log
    # token 只经环境变量传递：命令行里只能看到 token_env。
    assert "token_env=ASTSIGHT_AGENT_TOKEN" in sut_log
    assert "token=" not in sut_log
