"""`ruleset.json` 的加载、校验、原子保存与导入编排。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from contracts import (
    ContractRule,
    Rule,
    RuleAuthority,
    RuleConflict,
    RuleSet,
    RuleStatus,
)

from ..cases.requirements import normalize_text
from ..persistence import load_json, save_json_atomic, sha256_json, sha256_text
from .contract import build_contract_rules
from .diff import RuleDiff, diff_rules
from .draft import RuleDraft
from .grounding import rule_value_tokens
from .validate import RuleValidation, build_rule_set, contract_rule_key, rule_fingerprint, rule_key, validate_rule_draft

SUPPORTED_RULESET_SCHEMA = 2
MAX_RULESET_BYTES = 20 * 1024 * 1024
_KEY_RE = re.compile(r"^RK-[0-9a-f]{12}$")
_RULE_ID_RE = re.compile(r"^(BR|CR)-\d{2,}$")


class RuleStoreError(RuntimeError):
    """规则库不可加载/不可信。"""


@dataclass
class RuleImportResult:
    ruleset: RuleSet
    validation: RuleValidation
    diff: RuleDiff
    path: Path | None = None
    written: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "path": str(self.path) if self.path else None,
            "version": self.ruleset.version,
            "ruleset_id": self.ruleset.ruleset_id,
            "hash": rule_set_hash(self.ruleset),
            "validation": self.validation.as_dict(),
            "diff": self.diff.as_dict(),
        }


def rule_set_hash(ruleset: RuleSet) -> str:
    return sha256_json(ruleset.model_dump(mode="json"))


def rule_set_snapshot(ruleset: RuleSet) -> dict[str, Any]:
    return {
        "id": ruleset.ruleset_id,
        "version": ruleset.version,
        "hash": rule_set_hash(ruleset),
    }


def empty_rule_set(project: str) -> RuleSet:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return RuleSet(
        schema_version=SUPPORTED_RULESET_SCHEMA,
        ruleset_id="rs-empty",
        project=project,
        version=0,
        generated_at=now,
    )


def validate_rule_set(ruleset: RuleSet) -> list[str]:
    errors: list[str] = []
    if ruleset.schema_version != SUPPORTED_RULESET_SCHEMA:
        errors.append(
            f"ruleset_schema_unsupported: {ruleset.schema_version} "
            f"(supported={SUPPORTED_RULESET_SCHEMA})"
        )
    if not ruleset.project:
        errors.append("ruleset.project 为空")
    if not ruleset.ruleset_id:
        errors.append("ruleset.ruleset_id 为空")
    seen: set[str] = set()
    br_ids: set[str] = set()
    cr_ids: set[str] = set()
    for index, rule in enumerate(ruleset.rules):
        where = f"rules[{index}]"
        if not _KEY_RE.match(rule.rule_key):
            errors.append(f"{where}: 非法 rule_key: {rule.rule_key!r}")
        expected_key = rule_key(rule.source.doc, rule.source.section, rule.text)
        if rule.rule_key != expected_key:
            errors.append(
                f"{where}: rule_key 与内容不一致（期望 {expected_key}）"
            )
        if rule.rule_key in seen:
            errors.append(f"{where}: 重复 rule_key {rule.rule_key}")
        seen.add(rule.rule_key)
        if rule.rule_id in br_ids:
            errors.append(f"{where}: 重复 rule_id {rule.rule_id}")
        if not _RULE_ID_RE.match(rule.rule_id or ""):
            errors.append(f"{where}: 非法 rule_id {rule.rule_id!r}")
        br_ids.add(rule.rule_id)
        if rule.authority not in {RuleAuthority.A, RuleAuthority.B}:
            errors.append(f"{where}: BR authority 只能是 A/B")
        if rule.status is RuleStatus.UNGROUNDED and rule.authority is RuleAuthority.A:
            errors.append(f"{where}: ungrounded 规则不能是 A 级")
        if not rule.source.doc or not rule.source.section:
            errors.append(f"{where}: 来源 doc/section 为空")
        if rule.constraints and rule.status is RuleStatus.ACTIVE:
            quote_norm = normalize_text(rule.source.quote)
            missing = [
                token
                for token in rule_value_tokens("", rule.constraints)
                if normalize_text(token) not in quote_norm
            ]
            if missing:
                errors.append(
                    f"{where}: 结构化约束值不在 quote 中: {missing}"
                )
        expected_fp = rule_fingerprint(rule)
        if rule.fingerprint and rule.fingerprint != expected_fp:
            errors.append(f"{where}: fingerprint 与内容不一致")
        rule.fingerprint = expected_fp

    for index, rule in enumerate(ruleset.contract_rules):
        where = f"contract_rules[{index}]"
        if not _KEY_RE.match(rule.rule_key):
            errors.append(f"{where}: 非法 rule_key: {rule.rule_key!r}")
        expected_key = contract_rule_key(
            rule.transport.value, rule.endpoint, rule.field, rule.constraint
        )
        if rule.rule_key != expected_key:
            errors.append(
                f"{where}: rule_key 与内容不一致（期望 {expected_key}）"
            )
        if rule.rule_key in seen:
            errors.append(f"{where}: 重复 rule_key {rule.rule_key}")
        seen.add(rule.rule_key)
        if rule.rule_id in cr_ids:
            errors.append(f"{where}: 重复 rule_id {rule.rule_id}")
        if not _RULE_ID_RE.match(rule.rule_id or ""):
            errors.append(f"{where}: 非法 rule_id {rule.rule_id!r}")
        cr_ids.add(rule.rule_id)
        if rule.authority is not RuleAuthority.A:
            errors.append(f"{where}: CR authority 必须是 A")
        if not rule.endpoint:
            errors.append(f"{where}: endpoint 为空")

    known = {rule.rule_key for rule in ruleset.all_rules()}
    for index, conflict in enumerate(ruleset.conflicts):
        missing = [key for key in conflict.rule_keys if key not in known]
        if missing:
            errors.append(f"conflicts[{index}]: 引用了不存在的规则 {missing}")
        if len(set(conflict.rule_keys)) != len(conflict.rule_keys):
            errors.append(f"conflicts[{index}]: rule_keys 重复")
    return errors


def load_rule_set(path: str | Path) -> RuleSet:
    target = Path(path)
    if not target.is_file():
        raise RuleStoreError(f"规则库不存在: {target}")
    if target.stat().st_size > MAX_RULESET_BYTES:
        raise RuleStoreError(f"规则库过大: {target.stat().st_size} bytes")
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleStoreError(f"规则库不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuleStoreError("规则库根节点必须是 JSON object")
    if data.get("schema_version") != SUPPORTED_RULESET_SCHEMA:
        raise RuleStoreError(
            "ruleset_schema_unsupported: "
            f"{data.get('schema_version')!r} (supported={SUPPORTED_RULESET_SCHEMA})"
        )
    try:
        ruleset = RuleSet.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise RuleStoreError(f"规则库结构不合法: {exc}") from exc
    errors = validate_rule_set(ruleset)
    if errors:
        raise RuleStoreError("规则库校验失败:\n- " + "\n- ".join(errors))
    return ruleset


def save_rule_set(path: str | Path, ruleset: RuleSet) -> Path:
    errors = validate_rule_set(ruleset)
    if errors:
        raise RuleStoreError("拒绝保存非法规则库:\n- " + "\n- ".join(errors))
    return save_json_atomic(path, ruleset.model_dump(mode="json"))


def _merge_prompt(previous: RuleSet | None, prompt: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(previous.prompt) if previous else {}
    if prompt:
        merged.update({k: v for k, v in prompt.items() if v is not None})
    return merged


def _merge_origin(first: str, second: str) -> str:
    origins = {first or "model", second or "model"}
    if "model+framework" in origins:
        return "model+framework"
    if "model" in origins and "framework" in origins:
        return "model+framework"
    return sorted(origins)[0]


_SEVERITY_ORDER = {"loss": 0, "silent": 1, "rights": 2, "untestable": 3, "minor": 4}


def _conflict_key(conflict: RuleConflict) -> tuple[str, str]:
    """冲突身份 = topic + kind。

    只按 topic 合并会把"同一句话的两种不同毛病"压成一条：例如某个字段既
    存在枚举未闭合，又存在两处取值不一致，合并后只剩一条，另一条永久丢失。
    """
    return (conflict.topic, conflict.kind.value)


def _merge_severity(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return first if _SEVERITY_ORDER.get(first.value, 9) <= _SEVERITY_ORDER.get(
        second.value, 9
    ) else second


def _merge_conflicts(
    incoming: list[RuleConflict],
    known_keys: set[str],
) -> list[RuleConflict]:
    """按 (topic, kind) 合并**本次**抽取的冲突。

    每次导入都是一次完整抽取，不是增量补丁：上一版存在、本次不再检出的冲突
    必须消失。否则回写需求修复后重新抽取，旧冲突仍会留在规则库里，闭环永远
    收敛不了；无法回答的“它是不是已经修好了”也就无从判断。
    """
    by_key: dict[tuple[str, str], RuleConflict] = {}
    for raw in incoming:
        keys = sorted({key for key in raw.rule_keys if key in known_keys})
        if not keys:
            continue
        conflict = raw.model_copy(deep=True)
        conflict.rule_keys = keys
        existing = by_key.get(_conflict_key(conflict))
        if existing is None:
            by_key[_conflict_key(conflict)] = conflict
        else:
            existing.rule_keys = sorted(set(existing.rule_keys) | set(conflict.rule_keys))
            existing.docs = sorted(set(existing.docs) | set(conflict.docs))
            existing.origin = _merge_origin(existing.origin, conflict.origin)
            existing.severity = _merge_severity(existing.severity, conflict.severity)
            if conflict.reason and conflict.reason not in existing.reason:
                existing.reason = "; ".join(
                    part for part in (existing.reason, conflict.reason) if part
                )[:2_000]
    return [*by_key.values()]


def import_rule_draft(
    *,
    project: str,
    context: dict[str, Any],
    draft: RuleDraft,
    previous: RuleSet | None = None,
    contract_rules: Iterable[ContractRule] | None = None,
    prompt: dict[str, Any] | None = None,
    dry_run: bool = False,
    path: str | Path | None = None,
    allow_errors: bool = False,
) -> RuleImportResult:
    validation = validate_rule_draft(draft, context)
    if validation.errors and not allow_errors:
        ruleset = build_rule_set(
            project=project,
            context=context,
            validation=RuleValidation(context_id=validation.context_id),
            contract_rules=[],
            version=previous.version if previous else 1,
            ruleset_id=previous.ruleset_id if previous else None,
        )
        return RuleImportResult(
            ruleset=ruleset,
            validation=validation,
            diff=RuleDiff(),
            path=None,
            written=False,
        )

    if contract_rules is None:
        source = str((context.get("openapi") or {}).get("source") or "openapi")
        version = str((context.get("openapi") or {}).get("sha256") or "") or None
        contract_rules = build_contract_rules(
            context.get("operations") or [], source=source, source_version=version
        )
    contract_list = list(contract_rules)

    merged_conflicts = _merge_conflicts(
        [*validation.conflicts, *validation.framework_conflicts],
        {rule.rule_key for rule in [*validation.rules, *contract_list]},
    )
    version = (previous.version if previous else 0) + 1
    ruleset = build_rule_set(
        project=project,
        context=context,
        validation=RuleValidation(
            context_id=validation.context_id,
            rules=list(validation.rules),
            questions=list(validation.questions),
            gaps=list(validation.gaps),
            conflicts=merged_conflicts,
            errors=list(validation.errors),
            warnings=list(validation.warnings),
        ),
        contract_rules=contract_list,
        version=version,
        prompt=_merge_prompt(previous, prompt),
        ruleset_id=previous.ruleset_id if previous else None,
    )
    errors = validate_rule_set(ruleset)
    if errors:
        validation.errors.extend(errors)
        if not allow_errors:
            return RuleImportResult(
                ruleset=ruleset,
                validation=validation,
                diff=RuleDiff(),
                path=None,
                written=False,
            )
    diff = diff_rules(previous, ruleset.rules, ruleset.contract_rules)
    written_path = None
    written = False
    if path is not None and not dry_run and not errors:
        written_path = save_rule_set(path, ruleset)
        written = True
    elif path is not None and not dry_run:
        written_path = Path(path)
    return RuleImportResult(
        ruleset=ruleset,
        validation=validation,
        diff=diff,
        path=written_path,
        written=written,
    )


def rule_text(rule: Rule | ContractRule) -> str:
    if isinstance(rule, ContractRule):
        return f"{rule.endpoint} {rule.field}: {rule.constraint}"
    return rule.text


def rule_label(rule: Rule | ContractRule) -> str:
    return f"{rule.rule_id} ({rule.rule_key}, {rule.authority.value})"


__all__ = [
    "MAX_RULESET_BYTES",
    "SUPPORTED_RULESET_SCHEMA",
    "RuleImportResult",
    "RuleStoreError",
    "empty_rule_set",
    "import_rule_draft",
    "load_rule_set",
    "rule_label",
    "rule_set_hash",
    "rule_set_snapshot",
    "rule_text",
    "save_rule_set",
    "validate_rule_set",
]
