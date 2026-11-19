"""校验：mqmock。"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from contracts import (
    CaseLevel,
    CaseStep,
    DataCatalog,
    DubboCall,
    MockSemantic,
    PreconditionKind,
    RequirementDoc,
    RuleAuthority,
    RuleSet,
    RuleStatus,
    TestCase,
    Transport,
)
from .context import (
    context_documents,
    context_doc_versions,
    context_included_sections,
    context_requirements,
    find_profile,
    parse_endpoint,
)
from .data_refs import iter_data_refs, lookup_data
from .draft import (
    CATEGORY_RE,
    CASE_KEY_RE,
    MAX_BODY_DEPTH,
    MAX_BODY_ITEMS,
    STEP_NAME_RE,
    CaseDraft,
    DraftCase,
    DraftCaseStep,
    body_depth,
    body_item_count,
    case_fingerprint,
    to_test_case,
)
from .expressions import (
    field_paths,
    has_dynamic,
    is_step_ref,
    iter_dynamic_nodes,
    validate_dynamic,
)
from ..persistence import sha256_json
from .requirements import normalize_text
from .relations import materialize_rule_relations
from .strength import derive_assert_strength

from .validate_base import *


def _check_mq_assertions(
    draft_case: DraftCase,
    *,
    known_producers: set[tuple[str, str]],
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    """MQ 消息断言只校验生产者拦截结果，必须有 topic，dep_id 前缀固定。"""
    if draft_case.transport is Transport.MQ:
        result.errors.append(
            _issue(
                "error",
                "case.transport_mq_unsupported",
                "transport=mq 不能作为被测入口；MQ 只作为下游消息断言",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    seen_topics: set[str] = set()
    for index, item in enumerate(draft_case.mq_assertions):
        topic = str(item.topic or "").strip()
        if not topic or len(topic) > _MAX_DEP_ID_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mq.topic_invalid",
                    f"mq_assertions[{index}] 的 topic 为空或过长",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if topic in seen_topics:
            result.errors.append(
                _issue(
                    "error",
                    "mq.topic_duplicate",
                    f"mq_assertions 中 topic 重复: {topic!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        seen_topics.add(topic)
        client = str(item.client or "").strip().lower()
        if (
            known_producers
            and client in {"kafka", "rabbitmq", "rocketmq"}
            and (client, topic) not in known_producers
        ):
            result.warnings.append(
                _issue(
                    "warning",
                    "mq.topic_not_in_profile",
                    f"mq_assertions[{index}] 的 topic 未在画像生产者中扫描到: "
                    f"{client}/{topic}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if client not in {"kafka", "rabbitmq", "rocketmq"}:
            result.errors.append(
                _issue(
                    "error",
                    "mq.client_unsupported",
                    f"mq_assertions[{index}] 暂不支持 client={item.client!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        dep_id = str(item.dep_id or "").strip()
        if dep_id and (
            not dep_id.startswith("mq:") or len(dep_id) > _MAX_DEP_ID_LENGTH
        ):
            result.errors.append(
                _issue(
                    "error",
                    "mq.dep_id_invalid",
                    f"mq_assertions[{index}] 的 dep_id 必须以 mq: 开头",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if item.body is not None and (
            body_depth(item.body) > MAX_BODY_DEPTH
            or body_item_count(item.body) > MAX_BODY_ITEMS
        ):
            result.errors.append(
                _issue(
                    "error",
                    "mq.body_too_large",
                    f"mq_assertions[{index}] 的 body 超过深度/条目上限",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        for name, value in item.headers.items():
            if not str(name).strip() or len(str(name)) > 200:
                result.errors.append(
                    _issue(
                        "error",
                        "mq.header_invalid",
                        f"mq_assertions[{index}] 的 header 名非法: {name!r}",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
            if not isinstance(value, str):
                result.errors.append(
                    _issue(
                        "error",
                        "mq.header_invalid",
                        f"mq_assertions[{index}] 的 header 值必须是字符串",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )


def _check_mock_bindings(
    draft_case: DraftCase,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    """Mock 绑定进入执行会话，必须是可定位、非空的确定性字符串。"""
    if len(draft_case.mock_bindings) > _MAX_MOCK_BINDINGS:
        result.errors.append(
            _issue(
                "error",
                "mock.too_many_bindings",
                f"mock_bindings 数量 {len(draft_case.mock_bindings)} 超过上限 "
                f"{_MAX_MOCK_BINDINGS}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for dep_id, branch in draft_case.mock_bindings.items():
        if not dep_id.strip() or len(dep_id) > _MAX_DEP_ID_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mock.dep_id_invalid",
                    f"mock_bindings 的 dep_id 为空或过长: {dep_id[:80]!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    seen_rules: set[tuple[str, str]] = set()
    if (
        draft_case.transport.value != "http"
        and (draft_case.mock_rules or draft_case.mock_sequences)
    ):
        result.errors.append(
            _issue(
                "error",
                "mock.http_only_routing",
                "请求匹配和调用序列目前只支持 HTTP 下游 Mock",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for index, rule in enumerate(draft_case.mock_rules):
        if rule.dep_id not in draft_case.mock_bindings:
            result.errors.append(
                _issue(
                    "error",
                    "mock.rule_dep_missing_binding",
                    f"mock_rules[{index}] 的 dep_id 没有 default mock_binding: "
                    f"{rule.dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if not rule.branch.strip() or len(rule.branch) > _MAX_BRANCH_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mock.rule_branch_invalid",
                    f"mock_rules[{index}] 的 branch 为空或过长",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        matcher = rule.match.model_dump(mode="json")
        if not matcher.get("query") and not matcher.get("headers") and matcher.get("body") is None:
            result.errors.append(
                _issue(
                    "error",
                    "mock.rule_match_empty",
                    f"mock_rules[{index}] 至少需要 query/header/body 条件",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if rule.match.body is not None and (
            body_depth(rule.match.body) > MAX_BODY_DEPTH
            or body_item_count(rule.match.body) > MAX_BODY_ITEMS
        ):
            result.errors.append(
                _issue(
                    "error",
                    "mock.rule_match_body_too_large",
                    f"mock_rules[{index}] 的 body 匹配条件超过深度/条目上限",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        signature = (
            rule.dep_id,
            json.dumps(matcher, ensure_ascii=False, sort_keys=True),
        )
        if signature in seen_rules:
            result.errors.append(
                _issue(
                    "error",
                    "mock.rule_match_duplicate",
                    f"mock_rules 存在重复匹配条件: {rule.dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        seen_rules.add(signature)
    for dep_id, sequence in draft_case.mock_sequences.items():
        if dep_id not in draft_case.mock_bindings:
            result.errors.append(
                _issue(
                    "error",
                    "mock.sequence_dep_missing_binding",
                    f"mock_sequences 的 dep_id 没有 default mock_binding: {dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if not sequence:
            result.errors.append(
                _issue(
                    "error",
                    "mock.sequence_empty",
                    f"mock_sequences[{dep_id!r}] 不能为空",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        for branch in sequence:
            if not branch.strip() or len(branch) > _MAX_BRANCH_LENGTH:
                result.errors.append(
                    _issue(
                        "error",
                        "mock.sequence_branch_invalid",
                        f"mock_sequences[{dep_id!r}] 含空 branch 或超长 branch",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
        if not branch.strip() or len(branch) > _MAX_BRANCH_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mock.branch_invalid",
                    f"{dep_id[:80]!r} 的 branch 为空或过长: {branch[:80]!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
