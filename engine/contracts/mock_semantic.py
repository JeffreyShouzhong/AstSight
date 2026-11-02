"""C3.5.1 下游 Mock 语义契约。

契约层（dep_id/route/响应骨架）由画像确定性生成；业务分支可由规则映射，
规则不足时才由 LLM 候选，最终仍逐项校验。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import Evidence, MockStrategy, MockTransport, Transport
from .mock import MockStub


class MockBranchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="contract", max_length=64)
    rule_keys: list[str] = Field(default_factory=list, max_length=50)


class MockBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = Field(min_length=1, max_length=128)
    semantics: str = Field(min_length=1, max_length=2_000)
    stub: MockStub = Field(default_factory=MockStub)
    evidence: Evidence = Field(default=Evidence.INFERRED)
    source: MockBranchSource = Field(default_factory=MockBranchSource)


class MockDep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(min_length=1, max_length=512)
    transport: Transport = Field(default=Transport.HTTP)
    endpoint: str = Field(default="", max_length=512)
    route_key: str = Field(default="", max_length=200)
    strategy: MockStrategy = Field(default=MockStrategy.URL_OVERRIDE)
    property: str | None = Field(default=None, max_length=256)
    branches: list[MockBranch] = Field(default_factory=list, max_length=64)


class MockSemanticGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(default="", max_length=512)
    reason: str = Field(min_length=1, max_length=1_000)


class MockSemanticQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(default="", max_length=1_000)


class MockSemantic(BaseModel):
    """下游 Mock 语义集合（`mock_semantics.json`）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_id: str = Field(default="")
    generated_at: str = Field(default="")
    deps: list[MockDep] = Field(default_factory=list)
    gaps: list[MockSemanticGap] = Field(default_factory=list)
    questions: list[MockSemanticQuestion] = Field(default_factory=list)

    def branch_index(self) -> dict[tuple[str, str], MockBranch]:
        return {
            (dep.dep_id, branch.branch): branch
            for dep in self.deps
            for branch in dep.branches
        }

    def dep_index(self) -> dict[str, MockDep]:
        return {dep.dep_id: dep for dep in self.deps}


__all__ = [
    "MockBranch",
    "MockBranchSource",
    "MockDep",
    "MockSemantic",
    "MockSemanticGap",
    "MockSemanticQuestion",
]
