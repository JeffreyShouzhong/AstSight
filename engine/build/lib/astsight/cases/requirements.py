"""需求文档读取与 section 切分。

只做“文档 → 标题路径 + 原文 section”的事实整理，不做规则抽取/语义翻译。
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import sha256_text

_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}
_SKIP_DIRS = {".git", ".astsight", "__pycache__", ".mypy_cache", ".pytest_cache"}
# 需求文档是不可信输入：先限制单文件大小与文件数，避免把整个仓库/超大日志
# 读进 context，再指望 LLM 的 prompt 上限兜底。
MAX_DOC_BYTES = 2 * 1024 * 1024
MAX_DOC_FILES = 50
_METADATA_WORDS = (
    "版本",
    "版本信息",
    "版本记录",
    "版本历史",
    "修订记录",
    "修订历史",
    "变更记录",
    "变更日志",
    "更新记录",
    "目录",
    "附录",
    "术语",
    "术语表",
    "名词解释",
    "背景",
    "范围",
    "目标",
    "参考资料",
    "文档说明",
    "待确认",
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Markdown 分割线（***、---、___）不是正文；留在 section 里只会进 prompt、
# 污染 text_hash，对规则抽取没有任何信息量。fenced code 内的同类行保留。
_THEMATIC_BREAK_RE = re.compile(r"^\s*(?:\*{3,}|-{3,}|_{3,})\s*$")
_HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.、)]?\s*|[一二三四五六七八九十]+[、.)]\s*)"
)
_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")


class DocumentSection(BaseModel):
    """一个 Markdown 标题下的正文。"""

    model_config = ConfigDict(extra="forbid")

    section: str = Field(description="标题路径，如 '订单服务 > POST /api/v1/orders'")
    level: int = Field(default=0)
    text: str = Field(description="section 原文（不含标题行）")
    text_hash: str = Field(description="规范化正文 sha256")
    include: bool = Field(default=True, description="是否计入需求覆盖率分母")


class DocumentContent(BaseModel):
    """一份需求文档及其 section。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="项目相对 POSIX 路径")
    version: str = Field(description="文档 sha256")
    sections: list[DocumentSection] = Field(default_factory=list)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_metadata(title: str) -> bool:
    """元数据标题按“去编号后的整词”判断，不用子串匹配。

    子串匹配会把“目标库存策略”“背景音乐需求”这类真实需求一起排除，
    直接缩小覆盖率分母；宁可漏排除一个元数据标题，也不能误删需求。
    """
    core = _HEADING_NUMBER_RE.sub("", title or "").strip()
    core = _TRAILING_PAREN_RE.sub("", core).strip().rstrip("：:").strip()
    if core in _METADATA_WORDS:
        return True
    # “附录A”“附录 2”之类：词根 + 字母/数字后缀。
    return any(
        re.fullmatch(rf"{re.escape(word)}[A-Za-z0-9]+", core)
        for word in _METADATA_WORDS
    )


def _section_is_metadata(section: str) -> bool:
    return any(_is_metadata(part) for part in section.split(" > "))


def parse_sections(text: str) -> list[DocumentSection]:
    """按标题层级切分；忽略 fenced code block 内的 `#`。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    sections: list[DocumentSection] = []
    stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if not body:
            return
        path = " > ".join(title for _, title in stack) or "(文档开头)"
        sections.append(
            DocumentSection(
                section=path,
                level=stack[-1][0] if stack else 0,
                text=body,
                text_hash=sha256_text(normalize_text(body)),
                include=not _section_is_metadata(path),
            )
        )

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current_lines.append(line)
            continue
        if not in_fence and _THEMATIC_BREAK_RE.match(line):
            continue
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            flush()
            current_lines = []
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        current_lines.append(line)

    flush()
    return sections


def _resolve_input(raw: str, project: Path) -> Path:
    """相对路径先按 project 解析，再退回 cwd。

    `--project` 是显式给出的边界；如果 cwd 里恰好有同名文件，先取 cwd 会把
    文档指到项目外，然后在 `_ensure_inside` 里报一个与真实原因无关的错误。
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    project_candidate = (project / candidate).resolve()
    if project_candidate.exists():
        return project_candidate
    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    # 两处都不存在：返回 project 侧路径，让后续报“文档不存在”而不是越界。
    return project_candidate


def _ensure_inside(path: Path, project: Path) -> None:
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"需求文档必须位于项目内: {path}（project={project}）") from exc


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError(f"不支持的需求文档类型: {path}")
        return [path]
    files: list[Path] = []
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        if any(part in _SKIP_DIRS for part in candidate.relative_to(path).parts[:-1]):
            continue
        if candidate.suffix.lower() in _ALLOWED_SUFFIXES:
            files.append(candidate)
    return files


def read_documents(inputs: list[str], project: str | Path) -> list[DocumentContent]:
    """读取文件/目录输入，返回按项目相对路径排序的文档。"""
    project_path = Path(project).resolve()
    if not inputs:
        raise ValueError("至少提供一个需求文档/目录")

    seen: dict[Path, None] = {}
    for raw in inputs:
        resolved = _resolve_input(raw, project_path)
        _ensure_inside(resolved, project_path)
        if not resolved.exists():
            raise FileNotFoundError(f"需求文档不存在: {resolved}")
        for file_path in _iter_files(resolved):
            _ensure_inside(file_path.resolve(), project_path)
            seen[file_path.resolve()] = None

    if len(seen) > MAX_DOC_FILES:
        raise ValueError(f"需求文档过多: {len(seen)} 个 > 上限 {MAX_DOC_FILES}")

    documents: list[DocumentContent] = []
    for path in sorted(seen, key=lambda item: item.as_posix()):
        size = path.stat().st_size
        if size > MAX_DOC_BYTES:
            raise ValueError(
                f"需求文档过大: {path} ({size} bytes > {MAX_DOC_BYTES})"
            )
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"需求文档不是 UTF-8 文本: {path}") from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        documents.append(
            DocumentContent(
                doc=path.relative_to(project_path).as_posix(),
                version=sha256_text(text),
                sections=parse_sections(text),
            )
        )
    return documents


def document_fingerprint(documents: list[DocumentContent]) -> list[dict]:
    """context_id 用：文档 sha256 + 切分结果（路径/正文 hash/是否元数据）。

    只把文件 sha256 放进 context_id，会让切分或元数据规则变化后的 context
    沿用旧 id：同样的 id 对应两套 section，规则库和草案都无法察觉。
    """
    return [
        {
            "doc": document.doc,
            "version": document.version,
            "sections": [
                {
                    "path": section.section,
                    "text_hash": section.text_hash,
                    "include": section.include,
                }
                for section in document.sections
            ],
        }
        for document in documents
    ]


def section_map(document: DocumentContent) -> dict[str, DocumentSection]:
    return {section.section: section for section in document.sections}


def document_map(documents: list[DocumentContent]) -> dict[str, DocumentContent]:
    return {document.doc: document for document in documents}


__all__ = [
    "DocumentContent",
    "DocumentSection",
    "document_fingerprint",
    "document_map",
    "normalize_text",
    "parse_sections",
    "read_documents",
    "section_map",
]
