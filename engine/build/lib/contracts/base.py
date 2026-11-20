"""契约层公共基类与辅助类型。

提供带证据等级的字段包装，以及溯源标记的统一结构。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import Evidence


class Traced(BaseModel):
    """带证据等级的基础模型。

    所有"事实源"字段都应标注证据等级，保证"事实源零 LLM、不臆造"可被机器审计。
    """

    model_config = ConfigDict(extra="forbid")

    evidence: Evidence = Field(
        default=Evidence.UNMAPPED,
        description="证据等级：verified/inferred/unmapped",
    )


class SourceRef(BaseModel):
    """溯源引用（指向输入里真实存在的文档/章节/提交）。"""

    model_config = ConfigDict(extra="forbid")

    doc: str | None = Field(default=None, description="文档名")
    version: str | None = Field(default=None, description="版本号（文档 sha256）")
    section: str | None = Field(default=None, description="章节号/标题路径")
    quote: str | None = Field(
        default=None,
        description="需求原文片段，用于反幻觉校验与审核阅读；不参与文档版本漂移判断",
    )
