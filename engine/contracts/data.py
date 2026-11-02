"""C5 造数契约：数据目录（C5a）与单用例计划包（C5b）。

`DataCatalog` 只描述“有哪些表/列/可用业务态”，不产出逐用例 fixtures；
逐用例的 `DataPlan` 在用例批准后生成，定义在 `contracts.entities` 并在此复用。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import ColumnSemantic, DataWriteMode, Evidence
from .entities import DataPlan


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ValueSemantics(BaseModel):
    """字段取值含义；值必须来自 seed/枚举/需求。"""

    model_config = ConfigDict(extra="forbid")

    value: Any = None
    meaning: str = Field(min_length=1, max_length=500)
    evidence: Evidence = Field(default=Evidence.INFERRED)
    rule_keys: list[str] = Field(default_factory=list, max_length=50)


class BusinessState(BaseModel):
    """一个可被用例引用的业务状态。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    conditions: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default=Evidence.INFERRED)
    rule_keys: list[str] = Field(default_factory=list, max_length=50)


class ColumnCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    type: str = Field(default="", max_length=128)
    nullable: bool = Field(default=True)
    default: Any = None
    primary_key: bool = Field(default=False)
    unique: bool = Field(default=False)
    comment: str = Field(default="", max_length=1_000)
    enum_values: list[Any] = Field(default_factory=list, max_length=200)
    semantic_type: ColumnSemantic = Field(default=ColumnSemantic.UNKNOWN)
    value_semantics: list[ValueSemantics] = Field(default_factory=list, max_length=200)
    business_states: list[BusinessState] = Field(default_factory=list, max_length=100)
    evidence: Evidence = Field(default=Evidence.INFERRED)


class ForeignKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_table: str = Field(min_length=1, max_length=128)
    child_columns: list[str] = Field(min_length=1, max_length=20)
    parent_table: str = Field(min_length=1, max_length=128)
    parent_columns: list[str] = Field(min_length=1, max_length=20)
    evidence: Evidence = Field(default=Evidence.INFERRED)


class TableCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    purpose: str = Field(default="", max_length=1_000)
    read_write: DataWriteMode = Field(default=DataWriteMode.BOTH)
    columns: list[ColumnCatalog] = Field(default_factory=list, max_length=500)
    primary_key: list[str] = Field(default_factory=list, max_length=20)
    foreign_keys: list[ForeignKey] = Field(default_factory=list, max_length=200)
    comment: str = Field(default="", max_length=1_000)
    row_count_hint: int | None = Field(default=None, ge=0)
    evidence: Evidence = Field(default=Evidence.INFERRED)

    def column_index(self) -> dict[str, ColumnCatalog]:
        return {column.name: column for column in self.columns}


class FieldSemantics(BaseModel):
    """LLM 补全后的字段语义，独立于结构目录以便审计。"""

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    semantic_type: ColumnSemantic = Field(default=ColumnSemantic.UNKNOWN)
    value_semantics: list[ValueSemantics] = Field(default_factory=list)
    business_notes: str = Field(default="", max_length=2_000)
    evidence: Evidence = Field(default=Evidence.UNMAPPED)


class DataState(BaseModel):
    """A4 可引用的业务态；不包含逐用例 fixture。"""

    model_config = ConfigDict(extra="forbid")

    state_key: str = Field(min_length=1, max_length=128)
    target_state: dict[str, Any] = Field(default_factory=dict)
    table_values: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    evidence: Evidence = Field(default=Evidence.INFERRED)
    rule_keys: list[str] = Field(default_factory=list, max_length=50)


class DataCatalog(BaseModel):
    """C5a 数据目录。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    catalog_id: str = Field(default="")
    project: str = Field(default="")
    context_id: str | None = Field(default=None)
    generated_at: str = Field(default="")
    dialect: str = Field(default="sqlite")
    sources: dict[str, Any] = Field(default_factory=dict)
    tables: list[TableCatalog] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    tables_order: list[str] = Field(default_factory=list)
    field_semantics: list[FieldSemantics] = Field(default_factory=list)
    states: list[DataState] = Field(default_factory=list)
    seeds: list[dict[str, Any]] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def table_index(self) -> dict[str, TableCatalog]:
        return {table.name: table for table in self.tables}

    def state_index(self) -> dict[str, DataState]:
        return {state.state_key: state for state in self.states}

    def content_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))


class RejectedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    case_key: str = ""
    reason: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=2_000)


class DataPlanBundle(BaseModel):
    """`data_plan.json`：一次 run 的逐用例计划集合。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_scope: str = Field(default="")
    plans: list[DataPlan] = Field(default_factory=list)
    rejected: list[RejectedPlan] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "BusinessState",
    "ColumnCatalog",
    "DataCatalog",
    "DataPlanBundle",
    "DataState",
    "FieldSemantics",
    "ForeignKey",
    "RejectedPlan",
    "TableCatalog",
    "ValueSemantics",
]
