"""LLM 辅助的 Agent 流程：语义分析、用例生成、诊断与修复。

模型只产出候选；证据校验、用例校验、Mock 计划、审核与执行保持确定性。
"""
from .case_design import CaseDesignResult, generate_case_design
from .casegen import generate_case_draft, validate_case_bindings
from .data_prep import DataPlanDraft, generate_data_plans, generate_data_semantics
from .errors import AgentError
from .flow import FlowConfig, FlowResult, run_flow
from .mockplan import build_mock_plan, sync_mock_semantic
from .repair import RepairOutcome, diagnose_and_repair
from .rule_analysis import generate_rule_draft, import_rule_draft_for_flow
from .semantic import SemanticValidation, analyze_semantics, validate_semantics
from .sut import SutProcess, injection_env, start_sut

__all__ = [
    "AgentError",
    "CaseDesignResult",
    "DataPlanDraft",
    "FlowConfig",
    "FlowResult",
    "RepairOutcome",
    "SemanticValidation",
    "SutProcess",
    "analyze_semantics",
    "build_mock_plan",
    "diagnose_and_repair",
    "generate_case_design",
    "generate_case_draft",
    "generate_data_plans",
    "generate_data_semantics",
    "generate_rule_draft",
    "import_rule_draft_for_flow",
    "injection_env",
    "run_flow",
    "start_sut",
    "sync_mock_semantic",
    "validate_case_bindings",
    "validate_semantics",
]
