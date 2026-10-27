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


def _env(base_url: str | None, reset_url: str | None, timeout: float) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if base_url:
        env["ASTSIGHT_BASE_URL"] = base_url
    if reset_url:
        env["ASTSIGHT_RESET_URL"] = reset_url
    env["ASTSIGHT_TIMEOUT"] = str(timeout)
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
) -> tuple[bool, str]:
    """生成代码的收集自检：import/fixture 错误在开跑前暴露。"""
    cwd = Path(generated_dir).resolve()
    proc = _pytest(["--collect-only", "-q"], cwd, _env(base_url, reset_url, timeout), timeout)
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
) -> ExecutionResult:
    """执行 pytest（可选只跑指定 nodeids，用于有界重试）。"""
    cwd = Path(generated_dir).resolve()
    xml_path = Path(junit_path).resolve() if junit_path else cwd / "results.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["-q", "-p", "no:cacheprovider", f"--junitxml={xml_path}"]
    if maxfail:
        args.append(f"--maxfail={maxfail}")
    args.extend(nodeids or ["."])

    proc = _pytest(args, cwd, _env(base_url, reset_url, timeout), timeout)
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
