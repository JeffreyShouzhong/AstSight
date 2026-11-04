"""C1 规则上下文：需求 section + 可选 OpenAPI operation 摘要。

规则上下文不依赖用例上下文，也不调用模型；它只把不可信输入整理成稳定目录。
每个 section 同时有稳定 `path` 与可读 `number`，导入时二者都可引用。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cases.requirements import document_fingerprint, read_documents
from ..inventory import discover_openapi
from ..persistence import save_json_atomic
from ..profile import load_spec, parse_operations

RULES_CONTEXT_SCHEMA_VERSION = 1
RULES_PROMPT_VERSION = "6"
DEFAULT_RULES_OUT = ".astsight/rules"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_local_source(source: str | None, project: Path) -> str | None:
    if not source:
        return None
    if source.startswith(("http://", "https://")):
        return source
    path = Path(source)
    if not path.is_absolute():
        cwd_candidate = (Path.cwd() / path).resolve()
        path = cwd_candidate if cwd_candidate.exists() else (project / path).resolve()
    return str(path.resolve())


def _portable_source(source: str, project: Path) -> str:
    if source.startswith(("http://", "https://")):
        return source
    try:
        return Path(source).resolve().relative_to(project).as_posix()
    except ValueError:
        return source


@dataclass
class PreparedRulesContext:
    context_id: str
    context_dir: Path
    context: dict[str, Any]

    @property
    def context_path(self) -> Path:
        return self.context_dir / "context.json"

    @property
    def prompt_path(self) -> Path:
        return self.context_dir / "prompt.md"

    @property
    def schema_path(self) -> Path:
        return self.context_dir / "rules.schema.json"

    @property
    def readme_path(self) -> Path:
        return self.context_dir / "README.md"


def _documents_payload(documents: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for document in documents:
        sections: list[dict[str, Any]] = []
        for index, section in enumerate(document.sections, 1):
            sections.append(
                {
                    "section": section.section,
                    "path": section.section,
                    "number": str(index),
                    "level": section.level,
                    "text": section.text,
                    "text_hash": section.text_hash,
                    "include": section.include,
                }
            )
        payload.append(
            {
                "doc": document.doc,
                "version": document.version,
                "sections": sections,
            }
        )
    return payload


_PAGE_ID_RE = re.compile(r"P-\d{2}")
_SCOPE_PATH_HINTS = ("本次做", "本次不做", "需求范围")
_DETAIL_MIN_CHARS = 200
_STRUCTURE_MIN_CHARS = 2_000


def _declared_pages(sections: list[dict[str, Any]]) -> set[str]:
    """从「本次做」或「覆盖页面」声明中提取页面编号。

    只读取声明段落，不扫全文；否则「本次不做」里的 P-08/P-11 会被误当成
    本次范围，覆盖检查会永远通过。
    """
    scope_text = ""
    for section in sections:
        path = str(section.get("path") or section.get("section") or "")
        if "本次做" in path:
            scope_text += "\n" + str(section.get("text") or "")
    if not scope_text and sections:
        raw = str(sections[0].get("text") or "")
        match = re.search(r"覆盖页面[：:]\s*(.{0,300})", raw)
        if match:
            scope_text = match.group(1)
    return {match.group(0) for match in _PAGE_ID_RE.finditer(scope_text)}


def _detailed_pages(sections: list[dict[str, Any]]) -> set[str]:
    """从功能小节标题中提取页面编号。

    正文里顺带提到页面编号不算详情；只有标题带 P-xx 且内容达到最小长度的
    section 才算该页有详细说明。阈值用于排除只有一句跳转说明的占位标题。
    """
    pages: set[str] = set()
    for section in sections:
        path = str(section.get("path") or section.get("section") or "")
        text = str(section.get("text") or "")
        if len(text) < _DETAIL_MIN_CHARS:
            continue
        if any(hint in path for hint in _SCOPE_PATH_HINTS):
            continue
        pages.update(match.group(0) for match in _PAGE_ID_RE.finditer(path))
    return pages


def _structure_warnings(doc: str, sections: list[dict[str, Any]]) -> list[str]:
    """输入结构缺陷：章节塌缩、层级缺失、声明覆盖但正文没有详情。"""
    warnings: list[str] = []
    if not sections:
        return warnings
    total_chars = sum(len(str(section.get("text") or "")) for section in sections)
    paths = [
        str(section.get("path") or section.get("section") or "")
        for section in sections
    ]
    if len(sections) == 1 and total_chars >= _STRUCTURE_MIN_CHARS:
        # 整篇只有一个 section 时，quote 很容易退化成“全文任意子串”，
        # 反幻觉校验仍在，但证据粒度已经失真，必须让模型知道。
        warnings.append(f"sections_collapsed:{doc}")
    if total_chars >= _STRUCTURE_MIN_CHARS and not any(
        " > " in path for path in paths
    ):
        warnings.append(f"section_path_flat:{doc}")
    declared = _declared_pages(sections)
    missing = sorted(declared - _detailed_pages(sections))
    if missing:
        warnings.append(
            f"declared_scope_uncovered:{doc}:{','.join(missing)}"
        )
    return warnings


def _operations_payload(operations: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for operation in operations:
        out.append(
            {
                "operation_id": operation.operation_id,
                "method": operation.method,
                "path": operation.path,
                "endpoint": f"{operation.method} {operation.path}",
                "params": operation.params,
                "body_required": list(operation.body_required),
                "body_schema": operation.body_schema,
                "success_codes": list(operation.success_codes),
                "response_fields": operation.response_fields,
            }
        )
    return out


def prompt_digest(prompt_path: str | Path | None = None) -> str:
    """提示词内容 hash：提示词变了，context_id 必须变，旧上下文才会暴露为过期。"""
    if prompt_path is None:
        text = _prompt_template()
    else:
        path = Path(prompt_path)
        if not path.is_file():
            raise FileNotFoundError(f"规则提示词不存在: {path}")
        text = path.read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_rules_context(
    project: str | Path,
    requirements: list[str],
    *,
    openapi: str | None = None,
    prompt_path: str | Path | None = None,
) -> dict[str, Any]:
    project_path = Path(project).resolve()
    if not project_path.is_dir():
        raise ValueError(f"项目目录不存在: {project_path}")
    if not requirements:
        raise ValueError("至少提供一个 --requirements 文档/目录")
    documents = read_documents(requirements, project_path)
    if not documents:
        raise ValueError("需求文档目录中没有 .md/.markdown/.txt 文件")

    spec: dict[str, Any] = {}
    operations: list[Any] = []
    openapi_info: dict[str, Any] = {}
    source = discover_openapi(
        project_path,
        _resolve_local_source(openapi, project_path),
        None,
    )
    if source:
        spec = load_spec(source)
        operations = parse_operations(spec)
        openapi_info = {
            "source": _portable_source(source, project_path),
            "sha256": _sha(spec),
            "operation_count": len(operations),
            "title": str((spec.get("info") or {}).get("title") or ""),
            "version": str((spec.get("info") or {}).get("version") or ""),
        }

    document_payload = _documents_payload(documents)
    doc_versions = {document.doc: document.version for document in documents}
    prompt_info = {
        "name": "a1_requirement_analysis",
        "version": RULES_PROMPT_VERSION,
        "sha256": prompt_digest(prompt_path),
    }
    context_id = _sha(
        {
            "schema_version": RULES_CONTEXT_SCHEMA_VERSION,
            # 文档 sha256 + section 切分/元数据判定结果；切分规则变了，id 必须变。
            "documents": document_fingerprint(documents),
            "openapi_sha256": openapi_info.get("sha256", ""),
            "prompt_sha256": prompt_info["sha256"],
        }
    )
    warnings = [] if openapi_info else ["openapi_missing"]
    for document in document_payload:
        if not document["sections"]:
            # 空文档不会报错，但也不该静默：没有 section 就没有可引用原文，
            # 模型只能编造，必须在上下文里显式说明。
            warnings.append(f"document_empty:{document['doc']}")
        duplicates = sorted(
            path
            for path, count in Counter(
                section["path"] for section in document["sections"]
            ).items()
            if count > 1
        )
        # 同名标题会让 path 引用有歧义（resolve_section 只取第一个）；
        # 此时只有 number 能唯一定位，评审与排错都需要知道。
        warnings.extend(
            f"duplicate_section_path:{document['doc']}#{path}" for path in duplicates
        )
        warnings.extend(
            _structure_warnings(document["doc"], document["sections"])
        )
    return {
        "schema_version": RULES_CONTEXT_SCHEMA_VERSION,
        "context_id": context_id,
        "generated_at": _now(),
        "project": project_path.name,
        "documents": document_payload,
        "doc_versions": doc_versions,
        "openapi": openapi_info,
        "prompt": prompt_info,
        "operations": _operations_payload(operations),
        "warnings": warnings,
    }


def _prompt_template(prompt_path: str | Path | None = None) -> str:
    if prompt_path is not None:
        path = Path(prompt_path)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"规则提示词不存在: {path}")
    # prompts 与 engine 同级：AstSight/prompts
    path = Path(__file__).resolve().parents[3] / "prompts" / "a1_requirement_analysis.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "# A1 需求规则分析\n\n"
        "只从 context 的 section 原文提取业务规则。输出严格符合 rules.schema.json 的 JSON。\n"
    )


def _render_prompt(
    context: dict[str, Any],
    context_dir: Path,
    prompt_path: str | Path | None = None,
) -> str:
    text = _prompt_template(prompt_path)
    replacements = {
        "{{CONTEXT_ID}}": str(context.get("context_id") or ""),
        "{{CONTEXT_PATH}}": str(context_dir / "context.json"),
        "{{SCHEMA_PATH}}": str(context_dir / "rules.schema.json"),
        "{{DRAFT_PATH}}": str(context_dir / "rules.draft.json"),
        "{{DOCUMENTS}}": ", ".join(
            str(doc.get("doc")) for doc in context.get("documents") or []
        ),
        "{{OPENAPI}}": str((context.get("openapi") or {}).get("source") or ""),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _render_readme(context: dict[str, Any], context_dir: Path) -> str:
    return f"""# A1 规则上下文

- context_id: `{context.get('context_id')}`
- 文档: {", ".join(f"`{doc.get('doc')}`" for doc in context.get('documents') or [])}
- OpenAPI: `{(context.get('openapi') or {}).get('source', '')}`

## 下一步（模型在框架外运行时）

> 常规入口是 `astsight rules extract --requirements ...`，由框架内适配层调用模型；
> 本目录是同一流程的两段式拆分，供 Skill / 人工 / 模型网关 / 离线批处理使用。

1. 读取 `prompt.md`、`rules.schema.json` 和 `context.json`。
2. 生成 `rules.draft.json`（模型只提交候选，不写规则库）。
3. 运行：

```bash
python -m astsight rules import --project <project> \\
  --context "{context_dir}" --draft "{context_dir / 'rules.draft.json'}" \\
  --rule-base ruleset.json
```

导入执行与 `rules extract` 完全相同的确定性校验、CR 合并与版本化；
校验失败不写 `ruleset.json`。规则导入只写规则库；用例仍必须走 case proposal/人工审核。
"""


def write_rules_context(
    context: dict[str, Any],
    *,
    project: str | Path,
    out: str | Path = DEFAULT_RULES_OUT,
    prompt_path: str | Path | None = None,
) -> PreparedRulesContext:
    from .draft import RuleDraft

    project_path = Path(project).resolve()
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = project_path / out_path
    context_dir = out_path / str(context["context_id"])
    context_dir.mkdir(parents=True, exist_ok=True)
    save_json_atomic(context_dir / "context.json", context)
    save_json_atomic(context_dir / "rules.schema.json", RuleDraft.model_json_schema())
    (context_dir / "prompt.md").write_text(
        _render_prompt(context, context_dir, prompt_path),
        encoding="utf-8",
        newline="\n",
    )
    (context_dir / "README.md").write_text(
        _render_readme(context, context_dir), encoding="utf-8", newline="\n"
    )
    return PreparedRulesContext(
        context_id=str(context["context_id"]),
        context_dir=context_dir.resolve(),
        context=context,
    )


def prepare_rules_context(
    project: str | Path,
    requirements: list[str],
    *,
    openapi: str | None = None,
    out: str | Path = DEFAULT_RULES_OUT,
    prompt_path: str | Path | None = None,
) -> PreparedRulesContext:
    context = build_rules_context(
        project, requirements, openapi=openapi, prompt_path=prompt_path
    )
    return write_rules_context(
        context, project=project, out=out, prompt_path=prompt_path
    )


def load_rules_context(path: str | Path) -> dict[str, Any]:
    raw = Path(path)
    if raw.is_dir():
        raw = raw / "context.json"
    if not raw.is_file():
        raise FileNotFoundError(f"规则 context 不存在: {raw}")
    data = json.loads(raw.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"规则 context 不是 JSON object: {raw}")
    if data.get("schema_version") != RULES_CONTEXT_SCHEMA_VERSION:
        raise ValueError(f"不支持的规则 context schema_version: {data.get('schema_version')!r}")
    return data


def context_sections(context: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for document in context.get("documents") or []:
        doc = str(document.get("doc") or "")
        for section in document.get("sections") or []:
            path = str(section.get("path") or section.get("section") or "")
            index[(doc, path)] = section
    return index


def resolve_section(
    context: dict[str, Any], doc: str, section_or_number: str
) -> dict[str, Any] | None:
    """按 path 或 number 定位 section；二者都找不到返回 None。"""
    for document in context.get("documents") or []:
        if str(document.get("doc") or "") != doc:
            continue
        for section in document.get("sections") or []:
            if str(section.get("path") or section.get("section") or "") == section_or_number:
                return section
            if str(section.get("number") or "") == section_or_number:
                return section
    return None


__all__ = [
    "DEFAULT_RULES_OUT",
    "RULES_CONTEXT_SCHEMA_VERSION",
    "RULES_PROMPT_VERSION",
    "PreparedRulesContext",
    "build_rules_context",
    "context_sections",
    "load_rules_context",
    "prepare_rules_context",
    "prompt_digest",
    "resolve_section",
    "write_rules_context",
]
