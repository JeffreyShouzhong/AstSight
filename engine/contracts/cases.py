"""用例库、审核与需求覆盖契约。

`TestCase` 是执行形态；`CaseEntry` 在此基础上增加项目级版本与审核治理字段。
只有审核通过并写入 `caseset.json` 的用例才会进入 codegen/execution。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .entities import TestCase
from .enums import CaseStatus, ReviewDecision


class CaseReview(BaseModel):
    """一条用例最近一次人工审核决定。"""

    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    by: str | None = Field(default=None, description="审核人标识")
    at: str | None = Field(default=None, description="审核时间 ISO8601")
    note: str | None = Field(default=None, description="审核意见")
    fingerprint: str = Field(description="审核时的用例指纹")


class CaseEntry(TestCase):
    """项目级 case store 中的一条用例（批准或废弃）。"""

    case_key: str = Field(description="稳定业务标识，如 order_quantity_below_min")
    status: CaseStatus = Field(default=CaseStatus.APPROVED)
    version: int = Field(default=1, description="用例自身版本，从 1 递增")
    fingerprint: str = Field(description="行为/需求归属指纹")
    fingerprint_version: int = Field(default=1)
    review: CaseReview | None = Field(default=None, description="最近一次审核决定")
    created_at: str = Field(description="首次批准时间 ISO8601")
    updated_at: str = Field(description="最近更新时间 ISO8601")


class RequirementDoc(BaseModel):
    """需求文档目录快照；只保存计入覆盖率分母的 section。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="项目相对路径")
    version: str = Field(description="文档 sha256")
    sections: list[str] = Field(default_factory=list, description="标题路径列表")


class ReviewEvent(BaseModel):
    """追加式审核审计记录。"""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    case_id: str | None = Field(default=None, description="新用例批准前为空")
    case_key: str
    action: str = Field(description="new/update/revive/deprecate/unchanged")
    decision: ReviewDecision
    reviewer: str
    at: str = Field(description="决定时间 ISO8601")
    note: str | None = None
    fingerprint: str


class CaseSet(BaseModel):
    """项目级版本化用例库（`caseset.json`）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    fingerprint_version: int = 1
    id: str = Field(description="用例库标识")
    project: str = Field(description="项目标识/相对路径")
    store_version: int = Field(default=1, description="用例库版本，批准时 +1")
    sequence: int = Field(default=0, description="TC 编号高水位，只增不减")
    generated_at: str
    updated_at: str
    approved_at: str | None = None
    approved_by: str | None = None
    openapi: dict = Field(default_factory=dict, description="source/sha256/operation_count")
    cases: list[CaseEntry] = Field(default_factory=list, description="approved + deprecated")
    history: list[CaseEntry] = Field(default_factory=list, description="superseded 旧版本")
    review_log: list[ReviewEvent] = Field(default_factory=list)
    doc_versions: dict[str, str] = Field(default_factory=dict)
    requirements: list[RequirementDoc] = Field(default_factory=list)
    uncovered_reasons: dict[str, str] = Field(
        default_factory=dict, description="'doc::section' -> reason"
    )


__all__ = [
    "CaseEntry",
    "CaseReview",
    "CaseSet",
    "RequirementDoc",
    "ReviewEvent",
]
