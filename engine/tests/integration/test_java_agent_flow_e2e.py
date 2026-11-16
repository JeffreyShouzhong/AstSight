"""`agent run` 全链路：Java 画像 → Mock 计划 → JAVA_TOOL_OPTIONS 注入 → 执行 → 报告。

用 Spring Boot fat jar 样例做 SUT，经 `run_flow` 走真实编排（与 CLI 相同路径），
模型响应脚本化，因此断言的是确定性边界而不是模型发挥：

- 画像从 Java 源码识别 OkHttp / RestTemplate 的 HTTPS 出站并生成 dep_id；
- 两个调用点落在同一 host+method：OkHttp 走字面量 `/orders/7`，RestTemplate
  走模板 `/orders/{id}`；请求 `/orders/7` 时 agent 必须优先字面量路由，
  请求 `/orders/8` 时回落到模板，两边都不进 `/_agent/unmatched`；
- 计划选择 `agent_rewrite`，Mock 先起、SUT 后起；
- SUT 通过 `JAVA_TOOL_OPTIONS` + `token_env` 加载 agent，命令行不含明文 token；
- 证据显示请求确实经过 agent 改写到 `/_dep/<route_key>`，零透传；
- 报告门禁 pass。

启用：`ASTSIGHT_JAVA_AGENT_FLOW_E2E=1`，并先构建 agent jar 与样例 fat jar：
`cd java && mvn -q -DskipTests package`、
`cd java/samples/spring-boot-okhttp && mvn -q -DskipTests package`。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "spring-boot-okhttp"
DOC = "docs/requirements.md"
CALL_TEXT = "调用下游订单服务并返回状态码与响应体"
REST_TEXT = "按订单号调用下游订单服务并返回响应体"

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_FLOW_E2E") != "1",
    reason="agent run 全链路联调默认跳过：设置 ASTSIGHT_JAVA_AGENT_FLOW_E2E=1 启用",
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
        for path in (SAMPLE / "target").glob("spring-boot-okhttp-sample-*.jar")
        if not path.name.endswith(".original")
    ]
    if not jars:
        pytest.skip(
            "Spring Boot 样例未构建：cd java/samples/spring-boot-okhttp "
            "&& mvn -q -DskipTests package"
        )
    return sorted(jars)[0]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "java_flow"
    shutil.copytree(SAMPLE / "src", project / "src")
    shutil.copy2(SAMPLE / "pom.xml", project / "pom.xml")
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "requirements.md").write_text(
        "# 订单服务\n\n"
        "## GET /call\n\n"
        f"{CALL_TEXT}。\n\n"
        "## GET /rest/{id}\n\n"
        f"{REST_TEXT}。\n",
        encoding="utf-8",
    )
    return project


def _context(project: Path) -> dict:
    routes, _audit, _graph, _notes = inventory_routes(project, codegraph="off")
    return build_context(project, [DOC], routes=routes)


def _section(context: dict, endpoint: str) -> str:
    for document in context["documents"]:
        for section in document["sections"]:
            if section["section"].endswith(endpoint):
                return section["section"]
    raise AssertionError(f"section not found: {endpoint}")


def _dep_id(context: dict, endpoint: str) -> str:
    for profile in context["profiles"]:
        if profile["endpoint"] != endpoint:
            continue
        for dep in profile.get("downstream_deps") or []:
            return str(dep["dep_id"])
    raise AssertionError(f"downstream dep not found: {endpoint}")


def _rules(sections: dict[str, str]) -> dict:
    def rule(rule_id: str, text: str, section: str) -> dict:
        return {
            "text": text,
            "type": "other",
            "entities": ["订单"],
            "fields": [],
            "source": {
                "doc": DOC,
                "section": section,
                "quote": text,
                "kind": "explicit",
            },
            "authority": "A",
            "impact": "P1-功能",
            "priority": "P1",
            "rule_id": rule_id,
        }

    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "summary": "订单服务把下游状态码与响应体透传",
        "rules": [
            rule("BR-01", CALL_TEXT, sections["call"]),
            rule("BR-02", REST_TEXT, sections["rest"]),
        ],
        "questions": [],
        "gaps": [],
        "conflicts": [],
    }


def _mock_semantics(deps: dict[str, str], keys: dict[str, str]) -> dict:
    return {
        "deps": [
            {
                "dep_id": deps[name],
                "branches": [
                    {
                        "branch": "success",
                        "semantics": "下游返回订单",
                        "stub": {"status": 200, "body": {"ok": True}},
                        "evidence": "inferred",
                        "source": {"kind": "rule", "rule_keys": [keys[name]]},
                    }
                ],
            }
            for name in ("call", "rest")
        ],
        "gaps": [],
        "questions": [],
    }


def _draft(deps: dict[str, str], keys: dict[str, str], sections: dict[str, str]) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "cases": [
            {
                "case_key": "java_call_ok",
                "title": "OkHttp 出站经 agent 改写",
                "level": "business",
                "requirement_refs": [
                    {"doc": DOC, "section": sections["call"], "quote": CALL_TEXT}
                ],
                "rule_refs": [{"rule_key": keys["call"], "rule_id": "BR-01"}],
                "endpoint": "GET /call",
                "category": "positive",
                "asserts": [{"kind": "status_code", "op": "eq", "expect": 200}],
                "mock_bindings": {deps["call"]: "success"},
                "rationale": "下游 200 时 /call 返回 200",
            },
            {
                "case_key": "java_rest_ok",
                "title": "RestTemplate 模板路径经 agent 改写",
                "level": "business",
                "requirement_refs": [
                    {"doc": DOC, "section": sections["rest"], "quote": REST_TEXT}
                ],
                "rule_refs": [{"rule_key": keys["rest"], "rule_id": "BR-02"}],
                "endpoint": "GET /rest/{id}",
                "category": "positive",
                "path_params": {"id": 8},
                "asserts": [{"kind": "status_code", "op": "eq", "expect": 200}],
                "mock_bindings": {deps["rest"]: "success"},
                "rationale": "模板路径 /orders/8 命中下游模板路由",
            },
        ],
    }


def _config(project: Path, port: int, app_jar: Path) -> FlowConfig:
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
        ],
        approve_as="e2e",
        out=".astsight/agent",
        timeout=240.0,
        sut_timeout=60.0,
        allow_unmapped=True,
    )


def test_agent_run_rewrites_java_https_outbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(_agent_jar()))
    project = _project(tmp_path)
    context = _context(project)
    sections = {
        "call": _section(context, "GET /call"),
        "rest": _section(context, "GET /rest/{id}"),
    }
    deps = {
        "call": _dep_id(context, "GET /call"),
        "rest": _dep_id(context, "GET /rest/{id}"),
    }
    assert deps["call"].endswith("GET /orders/7")
    assert deps["rest"].endswith("GET /orders/{}")
    keys = {
        "call": rule_key(DOC, sections["call"], CALL_TEXT),
        "rest": rule_key(DOC, sections["rest"], REST_TEXT),
    }

    llm = ScriptedLLM(
        [
            _rules(sections),
            _mock_semantics(deps, keys),
            _draft(deps, keys, sections),
        ]
    )
    result = run_flow(_config(project, _free_port(), _sample_jar()), llm=llm)

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    run_dir = Path(result.run_dir)

    injection = json.loads((run_dir / "mock_injection.json").read_text("utf-8"))
    strategies = {
        item["dep_id"]: item["strategy"] for item in injection["injections"]
    }
    assert strategies == {
        deps["call"]: "agent_rewrite",
        deps["rest"]: "agent_rewrite",
    }

    records = json.loads((run_dir / "execution.json").read_text("utf-8"))["records"]
    assert len(records) == 2
    assert all(item["outcome"] == "passed" for item in records)

    calls = [
        json.loads(line)
        for line in (run_dir / "mock_calls.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    rewritten = [call for call in calls if call.get("mode") == "agent_rewrite"]
    assert len(rewritten) == 2, calls
    assert {call["adapter"] for call in rewritten} == {"okhttp", "resttemplate"}
    assert {call["original_host"] for call in rewritten} == {"api.example.com"}
    assert {call["original_path"] for call in rewritten} == {"/orders/7", "/orders/8"}
    assert all(call["result"] == "ok" for call in calls)
    # 字面量优先：/orders/7 不再与模板路由冲突。
    assert not any(call.get("error") == "mock_route_ambiguous" for call in calls)
    assert not any(call.get("error") == "mock_unexpected_dep" for call in calls)

    manifest = json.loads((run_dir / "run_manifest.json").read_text("utf-8"))
    assert manifest["token_fingerprint"]
    assert "token" not in manifest

    sut_log = (run_dir / "sut.log").read_text("utf-8", errors="replace")
    assert "Picked up JAVA_TOOL_OPTIONS" in sut_log
    assert "-javaagent:" in sut_log
    # token 只经环境变量传递：命令行里只能看到 token_env。
    assert "token_env=ASTSIGHT_AGENT_TOKEN" in sut_log
    assert "token=" not in sut_log
