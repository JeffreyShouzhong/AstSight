"""LLM 结构化输出契约（不可信输入，必须再做证据校验）。"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts import Assertion, MockStub

from ..cases.draft import DraftRequirementRef

MAX_BEHAVIORS = 200
MAX_BRANCHES = 200
MAX_QUESTIONS = 100
MAX_EVIDENCE = 8
MAX_PATCHES = 100

BEHAVIOR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
BRANCH_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class BehaviorDownstream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(min_length=1, max_length=512)
    branch: str = Field(min_length=1, max_length=128)
    why: str = Field(default="", max_length=1_000)


class SemanticBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    behavior_key: str
    endpoint: str = Field(description="METHOD /path")
    title: str = Field(default="", max_length=200)
    category: str = Field(default="business", max_length=64)
    description: str = Field(min_length=1, max_length=4_000)
    requirement_refs: list[DraftRequirementRef] = Field(min_length=1, max_length=20)
    expected_status: list[int] = Field(default_factory=list, max_length=20)
    downstream: list[BehaviorDownstream] = Field(default_factory=list, max_length=20)
    risk: str = Field(default="medium", max_length=32)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("behavior_key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not BEHAVIOR_KEY_RE.match(value):
            raise ValueError(f"behavior_key 不合法: {value!r}")
        return value

    @field_validator("expected_status")
    @classmethod
    def _statuses(cls, values: list[int]) -> list[int]:
        for value in values:
            if value < 100 or value > 599:
                raise ValueError(f"非法 HTTP 状态码: {value}")
        return list(dict.fromkeys(values))


class DownstreamBranchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(min_length=1, max_length=512)
    branch: str
    semantics: str = Field(min_length=1, max_length=2_000)
    stub: MockStub

    @field_validator("branch")
    @classmethod
    def _branch(cls, value: str) -> str:
        if not BRANCH_RE.match(value):
            raise ValueError(f"branch 不合法: {value!r}")
        return value


class SemanticAnalysis(BaseModel):
    """A1 语义分析输出：行为清单 + 下游分支语义。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_id: str
    summary: str = Field(default="", max_length=8_000)
    behaviors: list[SemanticBehavior] = Field(default_factory=list)
    downstream_branches: list[DownstreamBranchSpec] = Field(default_factory=list)
    uncovered_requirements: list[dict[str, str]] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, max_length=MAX_QUESTIONS)

    def branch_index(self) -> dict[tuple[str, str], DownstreamBranchSpec]:
        return {(item.dep_id, item.branch): item for item in self.downstream_branches}


class DiagnosisEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(max_length=2_000)
    source: str = Field(max_length=300)


class FailureDiagnosis(BaseModel):
    """A5 输出；只用于解释失败，不直接改写用例或审核决定。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=64)
    phenomenon: str = Field(min_length=1, max_length=2_000)
    root_cause: str
    action_target: str
    confidence: str
    evidence: list[DiagnosisEvidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    verify_action: str = Field(min_length=1, max_length=2_000)

    @field_validator("root_cause")
    @classmethod
    def _root(cls, value: str) -> str:
        allowed = {
            "code_defect", "case_error", "data_error", "mock_error",
            "change_induced", "adapter_error", "env_error",
        }
        if value not in allowed:
            raise ValueError(f"非法 root_cause: {value!r}")
        return value

    @field_validator("action_target")
    @classmethod
    def _target(cls, value: str) -> str:
        if value not in {"user", "framework"}:
            raise ValueError(f"非法 action_target: {value!r}")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: str) -> str:
        if value not in {"high", "medium", "low"}:
            raise ValueError(f"非法 confidence: {value!r}")
        return value


class CasePatch(BaseModel):
    """修复建议：只允许改请求/断言，不允许改身份和需求归属。"""

    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=3, max_length=64)
    reason: str = Field(min_length=1, max_length=2_000)
    path_params: dict[str, Any] | None = None
    query: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    body: Any = None
    asserts: list[Assertion] | None = None

    def changed_fields(self) -> list[str]:
        allowed = ("path_params", "query", "headers", "body", "asserts")
        return [name for name in allowed if name in self.model_fields_set]

    @field_validator("asserts")
    @classmethod
    def _asserts(cls, values: list[Assertion] | None):
        if values is not None and not values:
            raise ValueError("asserts 不能为空")
        if values is not None and len(values) > 50:
            raise ValueError("asserts 超过 50 条")
        return values


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    repairs: list[CasePatch] = Field(default_factory=list, max_length=MAX_PATCHES)


__all__ = [
    "BEHAVIOR_KEY_RE",
    "BRANCH_RE",
    "BehaviorDownstream",
    "CasePatch",
    "DiagnosisEvidence",
    "DownstreamBranchSpec",
    "FailureDiagnosis",
    "RepairPlan",
    "SemanticAnalysis",
    "SemanticBehavior",
]
