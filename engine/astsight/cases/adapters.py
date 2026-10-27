"""用例生成适配器。

v1 只实现“导入外部 Skill/人工产出的 JSON”；不引入任何 LLM SDK。
未来可新增 `LLMGenerator`，但下游校验/审核/版本化链路不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .context import PreparedContext, prepare_context
from .draft import CaseDraft, load_draft


@dataclass
class GenerationRequest:
    project: str
    requirements: list[str]
    openapi: str | None = None
    base_url: str | None = None
    out: str = ".astsight/casegen"
    extra: dict = field(default_factory=dict)


class CaseGenerator(Protocol):
    def prepare(self, request: GenerationRequest) -> PreparedContext: ...

    def load(self, context: dict, source: str | Path) -> CaseDraft: ...


class ImportGenerator:
    """读取 Skill/人工生成的 `cases.draft.json`。"""

    def prepare(self, request: GenerationRequest) -> PreparedContext:
        return prepare_context(
            request.project,
            request.requirements,
            openapi=request.openapi,
            base_url=request.base_url,
            out=request.out,
        )

    def load(self, context: dict, source: str | Path) -> CaseDraft:
        draft = load_draft(source)
        if draft.context_id and context.get("context_id"):
            if draft.context_id != context["context_id"]:
                raise ValueError(
                    f"草案 context_id={draft.context_id!r} 与 "
                    f"context={context['context_id']!r} 不一致"
                )
        return draft


class LLMGenerator:
    """预留接口；v1 明确不实现，避免新增模型/网络依赖。"""

    def prepare(self, request: GenerationRequest) -> PreparedContext:
        raise NotImplementedError("LLMGenerator 尚未实现；请使用 ImportGenerator")

    def load(self, context: dict, source: str | Path) -> CaseDraft:
        raise NotImplementedError("LLMGenerator 尚未实现；请使用 ImportGenerator")


def get_generator(kind: str = "import") -> CaseGenerator:
    if kind == "import":
        return ImportGenerator()
    if kind == "llm":
        return LLMGenerator()
    raise ValueError(f"未知生成器: {kind!r}")


__all__ = [
    "CaseGenerator",
    "GenerationRequest",
    "ImportGenerator",
    "LLMGenerator",
    "get_generator",
]
