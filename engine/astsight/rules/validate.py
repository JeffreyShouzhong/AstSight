"""规则草案的确定性校验与 rule_key/fingerprint 计算。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from contracts import (
    Priority,
    Rule,
    RuleAuthority,
    RuleConflict,
    RuleGap,
    RuleQuestion,
    RuleSet,
    RuleSource,
    RuleStatus,
    RuleType,
)

from ..cases.requirements import normalize_text
from .context import resolve_section
from .draft import RuleDraft

_RULE_ID_RE = re.compile(r"^BR-\d{2,}$")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_BACKTICK_RE = re.compile(r"`([^`]{2,80})`")
_MAX_RULES = 500


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def rule_key(doc: str, section: str, text: str) -> str:
    return "RK-" + _sha({"doc": doc, "section": section, "text": normalize_text(text)})[:12]


def contract_rule_key(
    transport: str, endpoint: str, field_name: str, constraint: str
) -> str:
    return "RK-" + _sha(
        {
            "transport": transport,
            "endpoint": endpoint,
            "field": field_name,
            "constraint": constraint,
        }
    )[:12]


def rule_fingerprint(rule: Rule) -> str:
    return _sha(rule.content_payload())


@dataclass
class RuleValidation:
    context_id: str
    rules: list[Rule] = field(default_factory=list)
    questions: list[RuleQuestion] = field(default_factory=list)
    gaps: list[RuleGap] = field(default_factory=list)
    conflicts: list[RuleConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "rules": len(self.rules),
            "questions": len(self.questions),
            "gaps": len(self.gaps),
            "conflicts": len(self.conflicts),
        }


def _ground_numbers(text: str, section_text: str) -> bool:
    section_norm = normalize_text(section_text)
    for token in _NUMBER_RE.findall(text or ""):
        if token not in section_norm:
            return False
    return True


def _ground_backticks(text: str, section_text: str) -> bool:
    section_norm = normalize_text(section_text)
    for token in _BACKTICK_RE.findall(text or ""):
        if normalize_text(token) not in section_norm:
            return False
    return True


def validate_rule_draft(draft: RuleDraft, context: dict[str, Any]) -> RuleValidation:
    result = RuleValidation(context_id=str(context.get("context_id") or ""))
    if draft.context_id and draft.context_id != result.context_id:
        result.errors.append(
            f"草案 context_id={draft.context_id!r} 与规则 context={result.context_id!r} 不一致"
        )
    if draft.schema_version != 1:
        result.errors.append(f"不支持的规则草案 schema_version={draft.schema_version}")
    if len(draft.rules) > _MAX_RULES:
        result.errors.append(f"规则数过多: {len(draft.rules)} > {_MAX_RULES}")

    seen: dict[str, int] = {}
    for index, item in enumerate(draft.rules):
        where = f"rules[{index}]"
        section_ref = item.source.section or item.source.number
        if not section_ref:
            result.errors.append(f"{where}: source.section/number 至少提供一个")
            continue
        section = resolve_section(context, item.source.doc, section_ref)
        if section is None:
            result.errors.append(
                f"{where}: 需求 section 不存在: {item.source.doc}#{section_ref}"
            )
            continue
        canonical_section = str(section.get("path") or section.get("section") or "")
        section_text = str(section.get("text") or "")
        quote = normalize_text(item.source.quote)
        status = RuleStatus.ACTIVE
        authority = item.authority
        if len(quote) < 4:
            result.warnings.append(f"{where}: 缺少可核对 quote，规则标 ungrounded")
            status = RuleStatus.UNGROUNDED
            authority = RuleAuthority.B
        elif quote not in normalize_text(section_text):
            result.errors.append(
                f"{where}: quote 不在 section 原文中: {item.source.quote!r}"
            )
            continue
        if item.authority not in {RuleAuthority.A, RuleAuthority.B}:
            result.warnings.append(
                f"{where}: A1 只允许 A/B，{item.authority.value} 已降为 B"
            )
            authority = RuleAuthority.B
        if authority is RuleAuthority.A and (
            not _ground_numbers(item.text, section_text)
            or not _ground_backticks(item.text, section_text)
        ):
            result.warnings.append(
                f"{where}: A 级规则中的数字/枚举未在 section 原文落地，降为 B"
            )
            authority = RuleAuthority.B
        key = rule_key(item.source.doc, canonical_section, item.text)
        if key in seen:
            result.errors.append(f"{where}: rule_key 与 rules[{seen[key]}] 重复: {key}")
            continue
        seen[key] = index
        rule = Rule(
            rule_key=key,
            rule_id=item.rule_id or "",
            text=normalize_text(item.text),
            type=item.type,
            entities=list(dict.fromkeys(item.entities)),
            fields=list(dict.fromkeys(item.fields)),
            source=RuleSource(
                doc=item.source.doc,
                section=canonical_section,
                kind=item.source.kind,
                version=str(context.get("doc_versions", {}).get(item.source.doc) or "") or None,
                doc_version=item.source.doc_version,
                quote=item.source.quote or None,
            ),
            authority=authority,
            impact=item.impact,
            priority=item.priority,
            reasoning=item.reasoning,
            status=status,
        )
        rule.fingerprint = rule_fingerprint(rule)
        result.rules.append(rule)

    # 稳定展示编号：优先保留合法 BR 编号，其余补号。
    used_ids = {
        rule.rule_id
        for rule in result.rules
        if _RULE_ID_RE.match(rule.rule_id or "")
    }
    next_id = 1
    for rule in result.rules:
        if _RULE_ID_RE.match(rule.rule_id or ""):
            continue
        while f"BR-{next_id:02d}" in used_ids:
            next_id += 1
        rule.rule_id = f"BR-{next_id:02d}"
        used_ids.add(rule.rule_id)
        next_id += 1

    for item in draft.questions:
        result.questions.append(
            RuleQuestion(
                id=item.id,
                text=item.text,
                reason=item.reason,
                impact=item.impact,
            )
        )
    for item in draft.gaps:
        result.gaps.append(
            RuleGap(
                kind=item.kind,
                desc=item.desc,
                reason=item.reason,
                related_interfaces=list(item.related_interfaces),
            )
        )

    by_id = {rule.rule_id: rule for rule in result.rules}
    by_key = {rule.rule_key: rule for rule in result.rules}
    for index, item in enumerate(draft.conflicts):
        keys = list(item.rule_keys)
        for rule_id in item.rule_ids:
            rule = by_id.get(rule_id)
            if rule is None:
                result.warnings.append(
                    f"conflicts[{index}]: 未找到规则 {rule_id!r}"
                )
            elif rule.rule_key not in keys:
                keys.append(rule.rule_key)
        unknown = [key for key in keys if key not in by_key]
        if unknown:
            result.warnings.append(f"conflicts[{index}]: 未找到 rule_key: {unknown}")
        for key in keys:
            if key in by_key:
                by_key[key].status = RuleStatus.DISPUTED
        result.conflicts.append(
            RuleConflict(
                topic=item.topic,
                rule_keys=keys,
                docs=list(item.docs),
                decision=item.decision,
            )
        )
    return result


def build_rule_set(
    *,
    project: str,
    context: dict[str, Any],
    validation: RuleValidation,
    contract_rules: list[Any],
    version: int = 1,
    prompt: dict[str, Any] | None = None,
    ruleset_id: str | None = None,
) -> RuleSet:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    documents = [
        {
            "doc": doc.get("doc"),
            "sha256": doc.get("version"),
            "sections": len(doc.get("sections") or []),
        }
        for doc in context.get("documents") or []
    ]
    return RuleSet(
        schema_version=1,
        ruleset_id=ruleset_id
        or ("rs-" + _sha({"project": project, "context_id": context.get("context_id")})[:16]),
        project=project,
        version=version,
        generated_at=now,
        prompt=dict(prompt or {}),
        documents=documents,
        rules=list(validation.rules),
        contract_rules=list(contract_rules),
        questions=list(validation.questions),
        gaps=list(validation.gaps),
        conflicts=list(validation.conflicts),
    )


__all__ = [
    "RuleValidation",
    "build_rule_set",
    "contract_rule_key",
    "rule_fingerprint",
    "rule_key",
    "validate_rule_draft",
]
