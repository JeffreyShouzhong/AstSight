"""CLI 命令共用的路径与可选输入加载。"""
from __future__ import annotations

from pathlib import Path

from contracts import DataCatalog, RuleSet

from ..persistence import load_json
from ..rules import load_rule_set


def _resolve_under_project(project: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        cwd_candidate = (Path.cwd() / path).resolve()
        path = cwd_candidate if cwd_candidate.exists() else (project / path).resolve()
    return path.resolve()


def _load_optional_ruleset(project: Path, raw: str | None) -> RuleSet | None:
    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return load_rule_set(path)


def _load_optional_catalog(project: Path, raw: str | None) -> DataCatalog | None:
    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return DataCatalog.model_validate(load_json(path))


def _load_optional_mock_semantic(project: Path, raw: str | None):
    from contracts import MockSemantic

    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return MockSemantic.model_validate(load_json(path))
