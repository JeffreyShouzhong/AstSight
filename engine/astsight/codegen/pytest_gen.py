"""C6 用例编码：把 TestCase 编译为 pytest 代码资产。

生成约定（对齐能力文档 C6）：
- 每个接口一个测试文件，类=接口，方法=用例；
- docstring 带用例编号/类别/溯源；
- 生成物头部带生成器版本与时间；
- `# ASTSIGHT MANUAL BEGIN/END` 段在二次生成时保留；
- 生成后做语法自检，失败即报告，不把坏代码交给执行层。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from contracts import TestCase

from ..artifacts import slugify, utc_now

GENERATOR = "AstSight M1 pytest-gen"

_CONFTEST = '''"""AstSight 生成物：HTTP 客户端与用例隔离。

手工修改请放在 MANUAL BEGIN/END 之间——二次生成会保留该段。
"""
import os

import httpx
import pytest

BASE_URL = os.environ.get("ASTSIGHT_BASE_URL", "http://127.0.0.1:8000")
RESET_URL = os.environ.get("ASTSIGHT_RESET_URL", "")
TIMEOUT = float(os.environ.get("ASTSIGHT_TIMEOUT", "10"))


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL.rstrip("/")


@pytest.fixture()
def client(base_url: str):
    # 可选的用例隔离钩子：被测项目提供重置入口时才启用。
    # 这不是 AstSight 的必需依赖，而是让"有状态服务"的正向用例可重复执行。
    if RESET_URL:
        # 既支持路径（/_reset），也支持完整 URL——后者便于 Git Bash 等
        # 会改写 "/" 开头参数的 shell 直接传参。
        reset = RESET_URL if RESET_URL.startswith(("http://", "https://")) else base_url + RESET_URL
        resp = httpx.post(reset, timeout=TIMEOUT)
        resp.raise_for_status()
    with httpx.Client(base_url=base_url, timeout=TIMEOUT) as http:
        yield http


# ASTSIGHT MANUAL BEGIN
# ASTSIGHT MANUAL END
'''

_PATH_PARAM_RE = re.compile(r"\{([^}/]+)\}|<[^:>]+:([^>]+)>|<([^>]+)>")


@dataclass
class CodegenResult:
    files: list[Path] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": [str(p) for p in self.files],
            "errors": self.errors,
            "skipped": self.skipped,
            "generator": GENERATOR,
            "generated_at": utc_now(),
        }


def _render_path(path: str, path_params: dict) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        if name not in path_params:
            return match.group(0)
        return quote(str(path_params[name]), safe="")

    return _PATH_PARAM_RE.sub(replace, path)


def _assert_line(assertion, case: TestCase) -> str:
    kind = assertion.kind
    op = assertion.op
    expect = assertion.expect
    if kind == "status_code":
        if op == "in":
            return (
                f"        assert resp.status_code in {expect!r}, "
                f"f\"expected status in {expect!r}, got {{resp.status_code}}: {{resp.text[:500]}}\""
            )
        if op == "between":
            low, high = expect
            return (
                f"        assert {low} <= resp.status_code <= {high}, "
                f"f\"expected status in [{low}, {high}], got {{resp.status_code}}: {{resp.text[:500]}}\""
            )
        return (
            f"        assert resp.status_code == {expect!r}, "
            f"f\"expected status {expect!r}, got {{resp.status_code}}: {{resp.text[:500]}}\""
        )

    if kind == "body_field":
        target = assertion.target or ""
        if op == "exists":
            return f"        assert {target!r} in body, f\"missing response field {target}\""
        if op == "eq":
            return (
                f"        assert body.get({target!r}) == {expect!r}, "
                f"f\"unexpected value for {target}: {{body.get({target!r})!r}}\""
            )
        if op == "ne":
            return f"        assert body.get({target!r}) != {expect!r}"
        if op == "in":
            return f"        assert body.get({target!r}) in {expect!r}"
        if op == "gte":
            return f"        assert body.get({target!r}) >= {expect!r}"
        if op == "lte":
            return f"        assert body.get({target!r}) <= {expect!r}"
        if op == "contains":
            return f"        assert {expect!r} in str(body.get({target!r}))"
    if kind == "header":
        target = assertion.target or ""
        if op == "exists":
            return f"        assert {target!r} in resp.headers"
        return f"        assert resp.headers.get({target!r}) == {expect!r}"
    return f"        # unsupported assertion: {kind}/{op}"


def _case_function(case: TestCase) -> str:
    name = f"test_{case.id.replace('-', '_').lower()}_{case.category}"
    path = _render_path(case.path, case.path_params)
    request_args = [repr(case.method), repr(path)]
    if case.query:
        request_args.append(f"params={case.query!r}")
    if case.headers:
        request_args.append(f"headers={case.headers!r}")
    if case.body is not None:
        request_args.append(f"json={case.body!r}")

    lines = [
        f"    def {name}(self, client):",
        f'        """case={case.id} endpoint={case.endpoint} category={case.category} '
        f'source={case.source}"""',
        f"        resp = client.request({', '.join(request_args)})",
    ]
    # 先判状态码再解析 body：服务返回 500/非 JSON 时，失败信息应指向真正的
    # 问题（状态码），而不是被 JSONDecodeError 掩盖成一条无上下文的异常。
    body_asserts = [a for a in case.asserts if a.kind == "body_field"]
    for assertion in case.asserts:
        if assertion.kind != "body_field":
            lines.append(_assert_line(assertion, case))
    if body_asserts:
        lines.append("        body = resp.json()")
        for assertion in body_asserts:
            lines.append(_assert_line(assertion, case))
    return "\n".join(lines)


def _class_name(method: str, path: str, used: set[str]) -> str:
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", path) if p]
    base = "Test" + method.title() + "".join(p.title() for p in parts)
    name, index = base, 2
    while name in used:
        name = f"{base}{index}"
        index += 1
    used.add(name)
    return name


def _manual_block(existing: str) -> str:
    match = re.search(
        r"# ASTSIGHT MANUAL BEGIN(.*?)# ASTSIGHT MANUAL END", existing, re.S
    )
    return match.group(1).strip("\n") if match else ""


def generate_pytest(
    cases: list[TestCase],
    output_dir: str | Path,
    *,
    generated_at: str | None = None,
) -> CodegenResult:
    """生成 conftest.py + 每接口一个测试文件，并做语法自检。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = CodegenResult()
    stamp = generated_at or utc_now()

    conftest = out / "conftest.py"
    existing = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    manual = _manual_block(existing)
    content = _CONFTEST.replace("# ASTSIGHT MANUAL BEGIN\n# ASTSIGHT MANUAL END",
                                f"# ASTSIGHT MANUAL BEGIN\n{manual}\n# ASTSIGHT MANUAL END")
    conftest.write_text(content, encoding="utf-8")
    result.files.append(conftest)

    grouped: dict[str, list[TestCase]] = {}
    for case in cases:
        grouped.setdefault(case.endpoint, []).append(case)

    used_classes: set[str] = set()
    for endpoint, endpoint_cases in grouped.items():
        method, _, path = endpoint.partition(" ")
        class_name = _class_name(method, path, used_classes)
        filename = f"test_{slugify(method.lower() + '_' + path)}.py"
        target = out / filename
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        manual = _manual_block(existing)

        header = (
            f'"""Auto-generated by {GENERATOR} at {stamp}.\n\n'
            f"Endpoint: {endpoint}\n"
            f"Cases: {len(endpoint_cases)}\n"
            f'Manual: 手工修改放在 ASTSIGHT MANUAL 段内。\n"""\n'
            f"import pytest\n\n\n"
        )
        body = "\n\n".join(_case_function(c) for c in endpoint_cases)
        footer = f"\n\n# ASTSIGHT MANUAL BEGIN\n{manual}\n# ASTSIGHT MANUAL END\n"
        source = f"{header}class {class_name}:\n{body}{footer}"

        try:
            compile(source, str(target), "exec")
        except SyntaxError as exc:
            result.errors.append(
                {"endpoint": endpoint, "error": f"{exc.msg} (line {exc.lineno})"}
            )
            continue

        target.write_text(source, encoding="utf-8")
        result.files.append(target)

    return result
