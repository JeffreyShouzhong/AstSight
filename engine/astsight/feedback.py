"""本地诊断包与 GitHub Issue 反馈。

设计边界：

- 只读运行产物，不执行被测代码、不联网；
- 默认脱敏后打包，使用者自行检查并附加到 GitHub Issue；
- 不包含 token、认证头、原始 query 值和绝对路径；
- 不自动上传，也不依赖任何服务端。
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from . import __version__

DEFAULT_REPO = "JeffreyShouzhong/AstSight"
MAX_BUNDLE_BYTES = 10 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_CASE_FILES = 20
MAX_FAILURES = 20
MAX_RECORDS = 100

_SENSITIVE_KEY = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|session[_-]?id|credential)",
    re.I,
)
_PATH_KEY = re.compile(r"(^|_)(path|file|filename|project|dir)$", re.I)
_URL_KEY = re.compile(r"(url|uri|endpoint|base_url|host)$", re.I)
_SAFE_HEADERS = {
    "accept",
    "content-type",
    "user-agent",
    "x-request-id",
    "x-correlation-id",
    "traceparent",
}
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
_AUTH_HEADER = re.compile(
    r"(?i)(authorization\s*[:=]\s*)([^\r\n,;]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|password|secret|api_key|apikey|key)=)[^&\s]+"
)
_ASSIGN_SECRET = re.compile(
    r"(?i)\b(token|access_token|password|secret|api_key|apikey|key)="
    r"[^&\s,;]+"
)
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scrub_text(text: Any) -> str:
    value = "" if text is None else str(text)
    value = _BEARER.sub(r"\1***", value)
    value = _AUTH_HEADER.sub(r"\1***", value)
    value = _URL_CREDENTIALS.sub(r"\1<credentials>@", value)
    value = _QUERY_SECRET.sub(r"\1***", value)
    value = _ASSIGN_SECRET.sub(r"\1=***", value)
    value = _EMAIL.sub("<email>", value)
    value = _PHONE.sub("<phone>", value)
    value = _ID_CARD.sub("<id>", value)
    return value


def scrub_url(value: Any) -> str:
    text = "" if value is None else str(value)
    try:
        parts = urlsplit(text)
    except ValueError:
        return scrub_text(text)
    if not parts.scheme or not parts.netloc:
        return scrub_text(text)
    query = "&".join(
        f"{key}=***"
        for key, _ in parse_qsl(parts.query, keep_blank_values=True)
    )
    host = "<host>"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def scrub_path(value: Any, *, project: str | None = None) -> str:
    text = scrub_text(value)
    for raw, label in (
        (project, "<project>"),
        (os.path.expanduser("~"), "<home>"),
    ):
        if raw:
            text = text.replace(str(raw), label)
            text = text.replace(str(raw).replace("\\", "/"), label)
    return text


def scrub_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    cleaned: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name)
        if key.lower() not in _SAFE_HEADERS:
            cleaned[key] = "***"
            continue
        cleaned[key] = scrub_text(str(value))[:200]
    return cleaned


def scrub_value(
    value: Any,
    *,
    key: str = "",
    project: str | None = None,
    depth: int = 0,
) -> Any:
    if depth > 8:
        return "<depth-limit>"
    if isinstance(value, dict):
        if key.lower() == "headers":
            return scrub_headers(value)
        cleaned: dict[str, Any] = {}
        for name, item in value.items():
            name = str(name)
            if _SENSITIVE_KEY.search(name):
                cleaned[name] = "***"
            else:
                cleaned[name] = scrub_value(
                    item,
                    key=name,
                    project=project,
                    depth=depth + 1,
                )
        return cleaned
    if isinstance(value, list):
        return [
            scrub_value(item, key=key, project=project, depth=depth + 1)
            for item in value[:200]
        ]
    if isinstance(value, str):
        if _URL_KEY.search(key) or value.startswith(("http://", "https://")):
            return scrub_url(value)
        if _PATH_KEY.search(key):
            return scrub_path(value, project=project)
        return scrub_text(value)
    return value


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dump(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _failure_ids(report: dict) -> list[str]:
    ids: list[str] = []
    for item in report.get("failures") or []:
        case_id = str(item.get("case_id") or "")
        if case_id and case_id not in ids:
            ids.append(case_id)
    for item in report.get("blocked") or []:
        case_id = str(item.get("case_id") or "")
        if case_id and case_id not in ids:
            ids.append(case_id)
    return ids


def _case_snapshot(
    run_dir: Path,
    *,
    case_ids: list[str],
    project: str | None,
) -> list[dict[str, Any]]:
    cases = _load_json(run_dir / "cases.json")
    if not isinstance(cases, list):
        return []
    wanted = set(case_ids)
    selected = [
        case
        for case in cases
        if not wanted or str(case.get("id") or "") in wanted
    ]
    if not wanted:
        selected = cases[:50]
    cleaned = []
    for case in selected[:50]:
        cleaned.append(
            {
                "id": case.get("id"),
                "title": scrub_text(case.get("title")),
                "endpoint": scrub_text(case.get("endpoint")),
                "category": case.get("category"),
                "transport": case.get("transport"),
                "assert_strength": case.get("assert_strength"),
                "rule_refs": case.get("rule_refs"),
                "requirement_refs": scrub_value(
                    case.get("requirement_refs"),
                    project=project,
                ),
            }
        )
    return cleaned


def _case_code(
    run_dir: Path,
    *,
    project: str | None,
    remaining: int,
) -> tuple[dict[str, bytes], list[str]]:
    entries: dict[str, bytes] = {}
    truncated: list[str] = []
    generated = run_dir / "generated"
    if not generated.is_dir():
        return entries, truncated
    files = sorted(generated.rglob("*.py"))[:MAX_CASE_FILES]
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(generated).as_posix()
        name = f"cases/{relative}"
        data = scrub_path(text, project=project).encode("utf-8")
        if len(data) > MAX_ENTRY_BYTES or total + len(data) > remaining:
            truncated.append(name)
            continue
        total += len(data)
        entries[name] = data
    if len(list(generated.rglob("*.py"))) > MAX_CASE_FILES:
        truncated.append("cases:max_files")
    return entries, truncated


def _summary_markdown(
    *,
    report: dict,
    framework: dict[str, str],
    truncated: list[str],
) -> str:
    run = report.get("run") or {}
    summary = report.get("summary") or {}
    gate = report.get("gate") or {}
    lines = [
        "# AstSight 诊断包",
        "",
        f"- 框架版本：`{framework['version']}`",
        f"- Python：`{framework['python']}`",
        f"- 平台：`{framework['platform']}`",
        f"- 运行 ID：`{run.get('run_id') or '-'}`",
        f"- 模式：`{run.get('mode') or '-'}`",
        f"- 门禁：**{str(gate.get('verdict') or '-').upper()}**",
        "",
        "## 结果摘要",
        "",
        f"- 执行用例：{summary.get('executed_cases', 0)}",
        f"- SUT 缺陷：{summary.get('sut_defects', 0)}",
        f"- 弱断言（weak_oracle）：{len(summary.get('weak_oracle') or [])}",
        f"- 未执行：{summary.get('unexecuted_cases', 0)}",
        f"- 数据阻断：{summary.get('blocked_cases', 0)}",
        "",
        "## 文件说明",
        "",
        "- `report.json`：脱敏后的运行报告",
        "- `execution.json`：脱敏后的执行记录",
        "- `mock_evidence.json`：脱敏后的 Mock 证据",
        "- `diagnostic.json`：结构化摘要与失败快照",
        "- `cases/`：失败用例代码（如包含）",
        "",
        "所有内容已在本地脱敏；提交前请再检查一遍。",
        "",
    ]
    if truncated:
        lines.append(
            "> 以下内容因大小限制未包含："
            + "、".join(f"`{item}`" for item in truncated)
        )
        lines.append("")
    return "\n".join(lines)


def _readme() -> str:
    return (
        "# 如何提交这份诊断包\n\n"
        "1. 打开 AstSight 的 GitHub Issues，选择 “Framework defect”。\n"
        "2. 填写现象、期望和最小复现步骤。\n"
        "3. 把本目录压缩成 zip（或直接使用反馈命令生成的 zip），拖入 issue。\n\n"
        "诊断包已在本地脱敏：token、认证头、query 值、邮箱/手机号和绝对路径"
        "不会原样包含。仍请在提交前自行检查一遍。\n"
    )


def collect_feedback(
    run_dir: str | Path,
    out: str | Path | None = None,
    *,
    include_case_code: bool = True,
    max_bytes: int = MAX_BUNDLE_BYTES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """生成脱敏诊断包；`dry_run` 只返回清单，不写文件。"""
    root = Path(run_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"run 目录不存在: {root}")
    report = _load_json(root / "report.json")
    if not isinstance(report, dict):
        raise ValueError(f"缺少或无法解析 report.json: {root / 'report.json'}")
    run_meta = report.get("run") or _load_json(root / "run_meta.json") or {}
    project = str(run_meta.get("project") or "") or None
    framework = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    failure_ids = _failure_ids(report)
    execution = _load_json(root / "execution.json") or {}
    if isinstance(execution, dict) and isinstance(execution.get("records"), list):
        execution = {
            **execution,
            "records": execution["records"][-MAX_RECORDS:],
        }
    mock_evidence = _load_json(root / "mock_evidence.json") or {}
    cleaned_report = scrub_value(report, project=project)
    cleaned_execution = scrub_value(execution, project=project)
    cleaned_mock = scrub_value(mock_evidence, project=project)
    cleaned_cases = _case_snapshot(
        root, case_ids=failure_ids, project=project
    )
    diagnostic = {
        "generated_at": _now(),
        "framework": framework,
        "run": scrub_value(run_meta, project=project),
        "gate": scrub_value(report.get("gate"), project=project),
        "summary": scrub_value(report.get("summary"), project=project),
        "audit": scrub_value(report.get("audit"), project=project),
        "failures": scrub_value(
            (report.get("failures") or [])[:MAX_FAILURES],
            project=project,
        ),
        "case_snapshot": cleaned_cases,
        "rules": scrub_value(report.get("rules"), project=project),
        "data_plan": scrub_value(report.get("data_plan"), project=project),
        "redaction": {
            "applied": True,
            "keys": "敏感键名、认证头、query 值、邮箱/手机号/身份证、绝对路径",
        },
    }

    files: dict[str, bytes] = {
        "README.md": _readme().encode("utf-8"),
        "diagnostic.json": _dump(diagnostic),
        "report.json": _dump(cleaned_report),
        "execution.json": _dump(cleaned_execution),
        "mock_evidence.json": _dump(cleaned_mock),
    }
    truncated: list[str] = []
    used = sum(len(data) for data in files.values())
    if include_case_code:
        code_entries, code_truncated = _case_code(
            root,
            project=project,
            remaining=max(0, max_bytes - used),
        )
        files.update(code_entries)
        truncated.extend(code_truncated)
    files["summary.md"] = _summary_markdown(
        report=report,
        framework=framework,
        truncated=truncated,
    ).encode("utf-8")

    kept: dict[str, bytes] = {}
    total = 0
    for name, data in files.items():
        if len(data) > MAX_ENTRY_BYTES or total + len(data) > max_bytes:
            truncated.append(name)
            continue
        kept[name] = data
        total += len(data)
    manifest = [
        {"name": name, "bytes": len(data)}
        for name, data in sorted(kept.items())
    ]
    result = {
        "run_dir": str(root),
        "files": manifest,
        "truncated": sorted(set(truncated)),
        "size": total,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    target = Path(out) if out else root / "feedback.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(kept.items()):
            bundle.writestr(name, data)
    result["path"] = str(target)
    return result


def build_issue_markdown(
    report: dict,
    *,
    framework_version: str,
    bundle_name: str = "feedback.zip",
) -> str:
    run = report.get("run") or {}
    summary = report.get("summary") or {}
    gate = report.get("gate") or {}
    failures = report.get("failures") or []
    lines = [
        "## 环境",
        "",
        f"- AstSight：`{framework_version}`",
        f"- 运行 ID：`{run.get('run_id') or '-'}`",
        f"- 模式：`{run.get('mode') or '-'}`",
        "",
        "## 现象",
        "",
        f"- 门禁：**{str(gate.get('verdict') or '-').upper()}**",
        f"- 执行 {summary.get('executed_cases', 0)} 条，"
        f"SUT 缺陷 {summary.get('sut_defects', 0)} 条，"
        f"弱断言 {len(summary.get('weak_oracle') or [])} 条",
        "",
    ]
    if failures:
        lines.append("失败明细（脱敏）：")
        lines.append("")
        for item in failures[:5]:
            lines.append(
                f"- `{item.get('case_id') or '-'}` "
                f"`{item.get('endpoint') or '-'}`："
                f"{item.get('reason') or item.get('message') or '-'}"
            )
        lines.append("")
    lines.extend(
        [
            "## 期望",
            "",
            "<!-- 你期望框架怎么处理？ -->",
            "",
            "## 最小复现步骤",
            "",
            "<!-- 命令、项目类型、必要的输入 -->",
            "",
            "## 诊断包",
            "",
            f"请把 `{bundle_name}` 拖入本 issue。"
            "它在本地生成并已脱敏，提交前请自行检查。",
            "",
        ]
    )
    return "\n".join(lines)


def _infer_repo(project: str | None) -> str:
    configured = os.environ.get("ASTSIGHT_REPO", "").strip()
    if configured:
        return configured
    if project:
        import subprocess

        try:
            completed = subprocess.run(
                ["git", "-C", project, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            raw = completed.stdout.strip()
            match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", raw)
            if match:
                return match.group(1)
    return DEFAULT_REPO


def open_issue(
    run_dir: str | Path,
    *,
    repo: str | None = None,
    out: str | Path | None = None,
    bundle: str | None = None,
) -> dict[str, Any]:
    """生成本地 issue.md 与 GitHub new-issue 链接；不自动上传。"""
    root = Path(run_dir).resolve()
    report = _load_json(root / "report.json")
    if not isinstance(report, dict):
        raise ValueError(f"缺少或无法解析 report.json: {root / 'report.json'}")
    run_meta = report.get("run") or {}
    project = str(run_meta.get("project") or "") or None
    resolved_repo = repo or _infer_repo(project)
    bundle_name = bundle or "feedback.zip"
    body = build_issue_markdown(
        report,
        framework_version=__version__,
        bundle_name=bundle_name,
    )
    title = (
        f"[framework-defect] {str((report.get('gate') or {}).get('verdict') or 'fail')}"
        f" {run_meta.get('run_id') or ''}"
    ).strip()
    issue_path = Path(out) if out else root / "issue.md"
    issue_path.write_text(
        f"# {title}\n\n{body}", encoding="utf-8"
    )
    excerpt = body
    while excerpt and len(quote(excerpt)) > 6000:
        excerpt = excerpt[: max(200, int(len(excerpt) * 0.8))]
    url = (
        f"https://github.com/{resolved_repo}/issues/new"
        f"?title={quote(title)}&body={quote(excerpt)}"
        f"&labels=framework-defect"
    )
    return {
        "repo": resolved_repo,
        "issue_md": str(issue_path),
        "url": url,
        "bundle": bundle_name,
        "body_bytes": len(body.encode("utf-8")),
        "truncated_url": len(excerpt) < len(body),
    }


__all__ = [
    "DEFAULT_REPO",
    "MAX_BUNDLE_BYTES",
    "build_issue_markdown",
    "collect_feedback",
    "open_issue",
    "scrub_headers",
    "scrub_path",
    "scrub_text",
    "scrub_url",
    "scrub_value",
]
