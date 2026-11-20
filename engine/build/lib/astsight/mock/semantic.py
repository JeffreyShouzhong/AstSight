"""C3.5.1 下游 Mock 语义构建与校验。

契约层（dep/route/默认 success）确定性生成；业务分支只从规则映射，
或由适配层补 candidates，再逐项校验。无依据的分支不生成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from contracts import (
    DubboMockReply,
    Evidence,
    GrpcMockReply,
    GrpcStatus,
    MockBranch,
    MockBranchSource,
    MockDep,
    MockOutcome,
    MockSemantic,
    MockSemanticGap,
    MockSemanticQuestion,
    MockStub,
    MockStrategy,
    Rule,
    RuleSet,
    RuleStatus,
    Transport,
)

_FAILURE_HINTS = (
    "失败",
    "错误",
    "异常",
    "超时",
    "拒绝",
    "不可用",
    "降级",
    "重试",
    "非法",
    "invalid",
    "error",
    "timeout",
    "fail",
    "reject",
    "unavailable",
)
_ERROR_RULES = {"error_handling", "state_transition", "data_consistency", "validation"}
_STATUS_RE = re.compile(r"(?<!\d)([45]\d{2})(?!\d)")
_MAX_BRANCHES = 64


class MockSemanticError(ValueError):
    """Mock 语义不合法。"""


@dataclass
class MockSemanticApplyResult:
    semantic: MockSemantic
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _dep_tokens(dep: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in (
        "service_name",
        "service",
        "grpc_path",
        "method",
        "target",
        "route_host",
        "path_key",
        "config_key",
        "property",
    ):
        value = str(dep.get(key) or "")
        if not value:
            continue
        if key in {"target"}:
            try:
                value = urlsplit(value).hostname or value
            except ValueError:
                pass
        tokens.update(
            part.lower()
            for part in re.split(r"[^A-Za-z0-9一-鿿]+", value)
            if len(part) >= 3
        )
    return tokens


def _token_in_text(token: str, text: str) -> bool:
    """按词匹配 dep token，避免 ``sms`` 命中错误码 ``SMS_COOLDOWN``。"""
    if token.isascii():
        pattern = rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])"
        return re.search(pattern, text) is not None
    return token in text


def _rule_matches_dep(rule: Rule, dep: dict[str, Any], *, only_dep: bool) -> bool:
    text = rule.text.lower()
    failed = any(hint in text for hint in _FAILURE_HINTS) or bool(
        _STATUS_RE.search(text)
    )
    if not failed:
        return False
    if any(token and _token_in_text(token, text) for token in _dep_tokens(dep)):
        return True
    if only_dep and any(
        word in text
        for word in ("下游", "依赖", "第三方", "降级", "超时", "服务")
    ):
        return True
    return False


def _value_for_type(type_text: str) -> Any:
    """按 A 的响应类型给 success 分支一个可解析的默认值。"""
    text = str(type_text or "").strip()
    if text.endswith("[]"):
        return []
    base = re.sub(r"<.*>", "", text).strip().lower()
    if base in {"string", "char", "character"}:
        return ""
    if base in {"boolean", "bool"}:
        return False
    if base in {"int", "integer", "long", "short", "byte"}:
        return 0
    if base in {"double", "float", "number", "bigdecimal", "biginteger"}:
        return 0.0
    if base in {
        "list", "set", "collection", "iterable", "arraylist", "linkedlist",
        "hashset", "linkedhashset",
    }:
        return []
    if base in {"map", "hashmap", "linkedhashmap", "jsonobject"}:
        return {}
    return {}


def _contract_body(raw: dict[str, Any] | None) -> Any:
    """从 A 的响应类型生成 success body；没有契约时返回 None。

    只做一层字段展开：嵌套对象给 `{}`，列表给 `[]`；标量按类型给
    `""`/`0`/`false`。不编造业务取值。
    """
    if not isinstance(raw, dict):
        return None
    contract = raw.get("response_contract")
    if not isinstance(contract, dict):
        return None
    for item in contract.get("types") or []:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") or []
        container = item.get("container")
        if fields:
            body: Any = {
                str(field.get("name")): _value_for_type(field.get("type", ""))
                for field in fields
                if isinstance(field, dict) and field.get("name")
            }
        elif item.get("kind") == "scalar":
            # String/数字/布尔等标量响应没有字段，按类型给默认值而不是 {}。
            body = _value_for_type(str(item.get("name") or ""))
        else:
            continue
        if container == "list":
            return [body]
        if container == "page":
            return {"content": [body], "totalElements": 1, "totalPages": 1}
        return body
    return None


def _dep_transport(raw: dict[str, Any]) -> Transport:
    dep_id = str(raw.get("dep_id") or "")
    dep_type = str(raw.get("dep_type") or "").lower()
    if dep_type == "dubbo" or dep_id.startswith("dubbo:"):
        return Transport.DUBBO
    if dep_type == "grpc" or dep_id.startswith("grpc:"):
        return Transport.GRPC
    return Transport.HTTP


def _default_branch(
    raw: dict[str, Any] | None = None,
    *,
    transport: Transport = Transport.HTTP,
) -> MockBranch:
    body = _contract_body(raw)
    if body is None and transport is Transport.GRPC and raw is not None:
        candidate = raw.get("response_schema")
        body = candidate if isinstance(candidate, (dict, list)) else None
    value = body if body is not None else {}
    reply = None
    if transport is Transport.DUBBO:
        reply = DubboMockReply(body=value)
    elif transport is Transport.GRPC:
        reply = GrpcMockReply(body=value)
    return MockBranch(
        branch="success",
        semantics=(
            "下游正常返回（字段取自 A 的响应类型）"
            if body is not None
            else "下游正常返回"
        ),
        outcome=MockOutcome.SUCCESS,
        reply=reply,
        stub=MockStub(status=200, body=value),
        evidence=Evidence.INFERRED,
        source=MockBranchSource(kind="contract", rule_keys=[]),
    )


def _rule_branch(rule: Rule, transport: Transport) -> MockBranch:
    branch = f"rule_{rule.rule_id}"
    match = _STATUS_RE.search(rule.text)
    status = int(match.group(1)) if match else 500
    body = {"error": "downstream_error", "rule_id": rule.rule_id}
    reply = None
    if transport is Transport.DUBBO:
        reply = DubboMockReply(body=body)
    elif transport is Transport.GRPC:
        body = {}
        grpc_status = GrpcStatus.INTERNAL
        if status == 400:
            grpc_status = GrpcStatus.INVALID_ARGUMENT
        elif status == 404:
            grpc_status = GrpcStatus.NOT_FOUND
        elif status in (502, 503, 504):
            grpc_status = GrpcStatus.UNAVAILABLE
        reply = GrpcMockReply(
            grpc_status=grpc_status,
            message=rule.text[:1_000],
            body=body,
        )
    return MockBranch(
        branch=branch,
        semantics=rule.text,
        outcome=MockOutcome.BUSINESS_ERROR,
        reply=reply,
        stub=MockStub(
            status=status if transport is Transport.HTTP else 200,
            body=body,
        ),
        evidence=Evidence.INFERRED,
        source=MockBranchSource(kind="rule", rule_keys=[rule.rule_key]),
    )


def build_mock_semantic(
    context: dict[str, Any],
    ruleset: RuleSet | None = None,
) -> MockSemantic:
    deps: dict[str, MockDep] = {}
    gaps: list[MockSemanticGap] = []
    profiles = context.get("profiles") or []
    active_rules = [
        rule
        for rule in (ruleset.rules if ruleset is not None else [])
        if rule.status is RuleStatus.ACTIVE and rule.type.value in _ERROR_RULES
    ]
    for profile in profiles:
        profile_deps = profile.get("downstream_deps") or []
        only_dep = len(profile_deps) == 1
        for raw in profile_deps:
            dep_id = str(raw.get("dep_id") or "")
            if not dep_id:
                continue
            transport = _dep_transport(raw)
            dep = deps.get(dep_id)
            if dep is None:
                method = str(raw.get("method") or "").upper()
                path_key = str(raw.get("path_key") or "")
                service = str(raw.get("service") or "")
                grpc_path = str(
                    raw.get("grpc_path")
                    or raw.get("path_key")
                    or raw.get("endpoint")
                    or ""
                )
                if transport is Transport.DUBBO:
                    endpoint = (
                        f"{service}#{raw.get('method')}"
                        if service and raw.get("method")
                        else dep_id
                    )
                    strategy = MockStrategy.AGENT_REWRITE
                elif transport is Transport.GRPC:
                    endpoint = grpc_path or dep_id
                    strategy = MockStrategy.GRPC_SERVER
                else:
                    endpoint = f"{method} {path_key}".strip()
                    strategy = MockStrategy.URL_OVERRIDE
                dep = MockDep(
                    dep_id=dep_id,
                    transport=transport,
                    endpoint=endpoint,
                    route_key=path_key if transport is Transport.HTTP else "",
                    strategy=strategy,
                    property=(
                        str((raw.get("config_keys") or [""])[0])
                        if raw.get("config_keys") and transport is Transport.HTTP
                        else None
                    ),
                    branches=[_default_branch(raw, transport=transport)],
                )
                deps[dep_id] = dep
            existing = {branch.branch for branch in dep.branches}
            for rule in active_rules:
                if not _rule_matches_dep(rule, raw, only_dep=only_dep):
                    continue
                branch = _rule_branch(rule, transport)
                if branch.branch in existing:
                    continue
                if len(dep.branches) >= _MAX_BRANCHES:
                    gaps.append(
                        MockSemanticGap(
                            dep_id=dep_id,
                            reason=f"branch_limit:{_MAX_BRANCHES}",
                        )
                    )
                    break
                dep.branches.append(branch)
                existing.add(branch.branch)
        if not profile_deps:
            gaps.append(
                MockSemanticGap(
                    dep_id="",
                    reason=f"downstream_unmapped:{profile.get('endpoint', '')}",
                )
            )
    return MockSemantic(
        schema_version=1,
        context_id=str(context.get("context_id") or ""),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        deps=sorted(deps.values(), key=lambda item: item.dep_id),
        gaps=gaps,
        questions=[],
    )


def _base_dep_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for profile in context.get("profiles") or []:
        for dep in profile.get("downstream_deps") or []:
            dep_id = str(dep.get("dep_id") or "")
            if dep_id:
                index.setdefault(dep_id, dep)
    return index


def apply_mock_semantic_candidates(
    semantic: MockSemantic,
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    ruleset: RuleSet | None = None,
) -> MockSemanticApplyResult:
    """合并适配层候选分支；只允许既有 dep，分支必须有规则依据。"""
    errors: list[str] = []
    warnings: list[str] = []
    deps = semantic.dep_index()
    known_context = _base_dep_index(context)
    known_rules = set(ruleset.rule_index()) if ruleset is not None else set()
    raw_deps = payload.get("deps") if isinstance(payload, dict) else None
    if not isinstance(raw_deps, list):
        errors.append("deps_not_list")
        return MockSemanticApplyResult(semantic, errors, warnings)

    for item in raw_deps:
        if not isinstance(item, dict):
            errors.append("dep_not_object")
            continue
        dep_id = str(item.get("dep_id") or "")
        dep = deps.get(dep_id)
        if dep is None:
            errors.append(f"dep_unknown:{dep_id}")
            continue
        if dep_id not in known_context:
            errors.append(f"dep_not_in_profile:{dep_id}")
            continue
        raw_branches = item.get("branches") or []
        if not isinstance(raw_branches, list):
            errors.append(f"branches_not_list:{dep_id}")
            continue
        existing = {branch.branch for branch in dep.branches}
        for raw in raw_branches:
            if not isinstance(raw, dict):
                errors.append(f"branch_not_object:{dep_id}")
                continue
            try:
                candidate = MockBranch.model_validate(raw)
            except Exception as exc:
                errors.append(f"branch_invalid:{dep_id}:{exc}")
                continue
            if candidate.branch in existing:
                if candidate.branch == "success" and candidate.source.rule_keys:
                    current = next(
                        branch
                        for branch in dep.branches
                        if branch.branch == candidate.branch
                    )
                    current.stub = candidate.stub
                    current.semantics = candidate.semantics
                    current.outcome = candidate.outcome
                    current.reply = candidate.reply
                    current.source = candidate.source
                    warnings.append(
                        f"branch_stub_overridden:{dep_id}:{candidate.branch}"
                    )
                    continue
                errors.append(f"branch_duplicate:{dep_id}:{candidate.branch}")
                continue
            if not candidate.source.rule_keys:
                errors.append(
                    f"branch_ungrounded:{dep_id}:{candidate.branch}"
                )
                continue
            unknown = [
                key for key in candidate.source.rule_keys if key not in known_rules
            ]
            if unknown:
                errors.append(
                    f"branch_rule_unknown:{dep_id}:{candidate.branch}:{unknown}"
                )
                continue
            if candidate.evidence is Evidence.VERIFIED:
                warnings.append(
                    f"branch_downgraded:{dep_id}:{candidate.branch}"
                )
                candidate.evidence = Evidence.INFERRED
            dep.branches.append(candidate)
            existing.add(candidate.branch)
        for gap in item.get("gaps") or []:
            if isinstance(gap, dict):
                semantic.gaps.append(
                    MockSemanticGap(
                        dep_id=dep_id,
                        reason=str(gap.get("reason") or "unknown")[:1_000],
                    )
                )
    # 框架外草案（MockSemanticDraft）把 gaps 放在顶层；这里与 per-dep gaps
    # 一起合并，避免模型明确声明的缺口被静默丢弃。
    for raw in payload.get("gaps") or []:
        if not isinstance(raw, dict):
            errors.append("gap_not_object")
            continue
        dep_id = str(raw.get("dep_id") or "")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            errors.append(f"gap_reason_missing:{dep_id or 'global'}")
            continue
        if dep_id and dep_id not in deps:
            errors.append(f"gap_dep_unknown:{dep_id}")
            continue
        semantic.gaps.append(
            MockSemanticGap(dep_id=dep_id, reason=reason[:1_000])
        )
    for raw in payload.get("questions") or []:
        if isinstance(raw, dict):
            try:
                semantic.questions.append(MockSemanticQuestion.model_validate(raw))
            except Exception as exc:
                errors.append(f"question_invalid:{exc}")
    return MockSemanticApplyResult(semantic, sorted(set(errors)), warnings)


def validate_mock_semantic(
    semantic: MockSemantic,
    context: dict[str, Any],
    *,
    ruleset: RuleSet | None = None,
) -> list[str]:
    errors: list[str] = []
    known_deps = _base_dep_index(context)
    known_rules = set(ruleset.rule_index()) if ruleset is not None else set()
    seen_deps: set[str] = set()
    for dep in semantic.deps:
        if dep.dep_id in seen_deps:
            errors.append(f"dep_duplicate:{dep.dep_id}")
        seen_deps.add(dep.dep_id)
        if known_deps and dep.dep_id not in known_deps:
            errors.append(f"dep_not_in_profile:{dep.dep_id}")
        if dep.transport not in (
            Transport.HTTP,
            Transport.DUBBO,
            Transport.GRPC,
        ):
            errors.append(f"transport_unsupported:{dep.dep_id}")
        if not dep.branches:
            errors.append(f"dep_no_branch:{dep.dep_id}")
        names: set[str] = set()
        for branch in dep.branches:
            if branch.branch in names:
                errors.append(f"branch_duplicate:{dep.dep_id}:{branch.branch}")
            names.add(branch.branch)
            if branch.reply is not None and branch.reply.transport != dep.transport.value:
                errors.append(
                    f"reply_transport_mismatch:{dep.dep_id}:{branch.branch}"
                )
            if dep.transport is Transport.GRPC and branch.reply is None:
                errors.append(
                    f"grpc_reply_required:{dep.dep_id}:{branch.branch}"
                )
            if dep.transport is Transport.HTTP and not (
                200 <= branch.stub.status <= 599
            ):
                errors.append(
                    f"branch_status_invalid:{dep.dep_id}:{branch.branch}"
                )
            if branch.source.kind == "rule" and not branch.source.rule_keys:
                errors.append(
                    f"branch_rule_missing:{dep.dep_id}:{branch.branch}"
                )
            for key in branch.source.rule_keys:
                if known_rules and key not in known_rules:
                    errors.append(
                        f"branch_rule_unknown:{dep.dep_id}:{branch.branch}:{key}"
                    )
    return errors


__all__ = [
    "MockSemanticApplyResult",
    "MockSemanticError",
    "apply_mock_semantic_candidates",
    "build_mock_semantic",
    "validate_mock_semantic",
]
