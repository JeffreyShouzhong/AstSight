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
_METADATA_WORDS = (
    "版本",
    "变更记录",
    "变更日志",
    "更新记录",
    "目录",
    "附录",
    "术语",
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
    return any(word in title for word in _METADATA_WORDS)


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
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (project / candidate).resolve()


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

    documents: list[DocumentContent] = []
    for path in sorted(seen, key=lambda item: item.as_posix()):
        text = path.read_text(encoding="utf-8-sig")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        documents.append(
            DocumentContent(
                doc=path.relative_to(project_path).as_posix(),
                version=sha256_text(text),
                sections=parse_sections(text),
            )
        )
    return documents


def section_map(document: DocumentContent) -> dict[str, DocumentSection]:
    return {section.section: section for section in document.sections}


def document_map(documents: list[DocumentContent]) -> dict[str, DocumentContent]:
    return {document.doc: document for document in documents}


__all__ = [
    "DocumentContent",
    "DocumentSection",
    "document_map",
    "normalize_text",
    "parse_sections",
    "read_documents",
    "section_map",
]
