"""pytest 执行与结果解析。

执行层零 LLM：只负责“跑”和“把结果变成结构化记录”。
报告合并、归因、重试决策在 diagnose / report 模块。

输出用 JUnit XML（pytest 内置），不引入额外插件——执行层的依赖越少越稳。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_CASE_ID_RE = re.compile(r"tc_(\d+)", re.I)


@dataclass
class TestRecord:
    nodeid: str
    case_id: str
    classname: str
    name: str
    outcome: str = "unknown"      # passed/failed/error/skipped
    duration: float = 0.0
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "case_id": self.case_id,
            "classname": self.classname,
            "name": self.name,
            "outcome": self.outcome,
            "duration": self.duration,
            "message": self.message,
        }


@dataclass
class ExecutionResult:
    returncode: int
    records: list[TestRecord] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    collection_errors: list[str] = field(default_factory=list)
    junit_path: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for r in self.records if r.outcome == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if r.outcome in ("failed", "error"))

    def as_dict(self) -> dict:
        return {
            "returncode": self.returncode,
            "total": len(self.records),
            "passed": self.passed,
            "failed": self.failed,
            "collection_errors": self.collection_errors,
            "junit_path": self.junit_path,
        }


_MOCK_ENV = {
    "mock_admin_url": "ASTSIGHT_MOCK_ADMIN_URL",
    "mock_token": "ASTSIGHT_MOCK_TOKEN",
    "mock_namespace": "ASTSIGHT_MOCK_NAMESPACE",
    "mock_run_scope": "ASTSIGHT_MOCK_RUN_SCOPE",
    "mock_drain_ms": "ASTSIGHT_MOCK_DRAIN_MS",
    "mock_ttl_ms": "ASTSIGHT_MOCK_TTL_MS",
}


def _env(
    base_url: str | None,
    reset_url: str | None,
    timeout: float,
    *,
    mock_admin_url: str | None = None,
    mock_token: str | None = None,
    mock_namespace: str | None = None,
    mock_run_scope: str | None = None,
    mock_drain_ms: int | None = None,
    mock_ttl_ms: int | None = None,
    data_plan_path: str | None = None,
    database_url: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if base_url:
        env["ASTSIGHT_BASE_URL"] = base_url
    if reset_url:
        env["ASTSIGHT_RESET_URL"] = reset_url
    env["ASTSIGHT_TIMEOUT"] = str(timeout)
    # 造数计划路径与数据库 URL 只通过环境变量传给生成物，不写入报告。
    if data_plan_path:
        env["ASTSIGHT_DATA_PLAN_PATH"] = data_plan_path
    if database_url:
        env["ASTSIGHT_DB_URL"] = database_url
    # 未显式配置时保留调用方环境变量；显式值优先。
    for name, value in {
        "mock_admin_url": mock_admin_url,
        "mock_token": mock_token,
        "mock_namespace": mock_namespace,
        "mock_run_scope": mock_run_scope,
        "mock_drain_ms": mock_drain_ms,
        "mock_ttl_ms": mock_ttl_ms,
    }.items():
        if value is not None:
            env[_MOCK_ENV[name]] = str(value)
    return env


def _pytest(args: list[str], cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


def collect_only(
    generated_dir: str | Path,
    *,
    base_url: str | None = None,
    reset_url: str | None = None,
    timeout: float = 300.0,
    mock_admin_url: str | None = None,
    mock_token: str | None = None,
    mock_namespace: str | None = None,
    mock_run_scope: str | None = None,
    mock_drain_ms: int | None = None,
    mock_ttl_ms: int | None = None,
    data_plan_path: str | None = None,
    database_url: str | None = None,
) -> tuple[bool, str]:
    """生成代码的收集自检：import/fixture 错误在开跑前暴露。"""
    cwd = Path(generated_dir).resolve()
    proc = _pytest(
        ["--collect-only", "-q"],
        cwd,
        _env(
            base_url,
            reset_url,
            timeout,
            mock_admin_url=mock_admin_url,
            mock_token=mock_token,
            mock_namespace=mock_namespace,
            mock_run_scope=mock_run_scope,
            mock_drain_ms=mock_drain_ms,
            mock_ttl_ms=mock_ttl_ms,
            data_plan_path=data_plan_path,
            database_url=database_url,
        ),
        timeout,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def run_pytest(
    generated_dir: str | Path,
    *,
    base_url: str | None = None,
    reset_url: str | None = None,
    junit_path: str | Path | None = None,
    nodeids: list[str] | None = None,
    timeout: float = 600.0,
    maxfail: int | None = None,
    mock_admin_url: str | None = None,
    mock_token: str | None = None,
    mock_namespace: str | None = None,
    mock_run_scope: str | None = None,
    mock_drain_ms: int | None = None,
    mock_ttl_ms: int | None = None,
    data_plan_path: str | None = None,
    database_url: str | None = None,
) -> ExecutionResult:
    """执行 pytest（可选只跑指定 nodeids，用于有界重试）。"""
    cwd = Path(generated_dir).resolve()
    xml_path = Path(junit_path).resolve() if junit_path else cwd / "results.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["-q", "-p", "no:cacheprovider", f"--junitxml={xml_path}"]
    if maxfail:
        args.append(f"--maxfail={maxfail}")
    args.extend(nodeids or ["."])

    proc = _pytest(
        args,
        cwd,
        _env(
            base_url,
            reset_url,
            timeout,
            mock_admin_url=mock_admin_url,
            mock_token=mock_token,
            mock_namespace=mock_namespace,
            mock_run_scope=mock_run_scope,
            mock_drain_ms=mock_drain_ms,
            mock_ttl_ms=mock_ttl_ms,
            data_plan_path=data_plan_path,
            database_url=database_url,
        ),
        timeout,
    )
    result = ExecutionResult(
        returncode=proc.returncode,
        stdout=(proc.stdout or "")[-20000:],
        stderr=(proc.stderr or "")[-20000:],
        junit_path=str(xml_path),
    )
    if xml_path.exists():
        result.records = _parse_junit(xml_path)
    else:
        result.collection_errors.append(
            "pytest 未生成 JUnit 报告（可能在收集阶段就失败）:\n"
            + (result.stderr or result.stdout)[-2000:]
        )
    return result


def _nodeid(classname: str, name: str) -> str:
    """JUnit 的 classname 是点分模块名，pytest 定向重跑要的是 `file.py::Class::test`。

    pytest 的 junit_family=xunit2 输出 `test_orders.TestOrders`，直接拼
    `test_orders.TestOrders::test_x` 给 pytest 会匹配不到用例，重试会静默失效。
    """
    if not classname:
        return name
    parts = classname.split(".")
    if len(parts) >= 2:
        file_part = parts[0] + ".py"
        return "::".join([file_part, *parts[1:], name])
    return f"{classname}::{name}"


def _parse_junit(path: Path) -> list[TestRecord]:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise RuntimeError(f"JUnit XML 解析失败: {exc}") from exc

    records: list[TestRecord] = []
    for testcase in tree.iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        try:
            duration = float(testcase.get("time", "0") or 0)
        except ValueError:
            duration = 0.0

        outcome = "passed"
        message = ""
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            outcome = "failed"
            message = (failure.get("message") or failure.text or "").strip()
        elif error is not None:
            outcome = "error"
            message = (error.get("message") or error.text or "").strip()
        elif skipped is not None:
            outcome = "skipped"
            message = (skipped.get("message") or "").strip()
            # 造数前置未满足是 blocked（不能算通过，也不是 SUT 缺陷）；
            # 与普通 skip 区分，报告才能单列。
            if message.lower().startswith("data_unavailable"):
                outcome = "blocked"

        match = _CASE_ID_RE.search(name)
        case_id = f"TC-{int(match.group(1)):04d}" if match else ""
        records.append(
            TestRecord(
                nodeid=_nodeid(classname, name),
                case_id=case_id,
                classname=classname,
                name=name,
                outcome=outcome,
                duration=duration,
                message=message[:4000],
            )
        )
    return records
