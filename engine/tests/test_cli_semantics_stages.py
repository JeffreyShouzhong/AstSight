"""CLI 冒烟：C5a / C3.5.1 的框架外草案与框架内 LLM 阶段。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cli import main
from astsight.data import build_catalog, parse_ddl
from astsight.data.semantics import SemanticDraft, apply_semantics
from contracts import DataState


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _context(tmp_path: Path) -> Path:
    return _write_json(
        tmp_path / "context.json",
        {
            "schema_version": 1,
            "context_id": "ctx-cli-semantics",
            "documents": [
                {
                    "doc": "docs/requirements.md",
                    "version": "v1",
                    "sections": [
                        {"section": "1", "number": "1", "text": "库存为 0 时不可下单"}
                    ],
                }
            ],
            "profiles": [
                {
                    "endpoint": "GET /orders/{order_id}",
                    "downstream_deps": [
                        {
                            "dep_id": "inventory",
                            "method": "GET",
                            "path_key": "/stock",
                        }
                    ],
                }
            ],
        },
    )


def test_mock_semantics_cli_builds_contract_layer(tmp_path: Path, capsys) -> None:
    context_path = _context(tmp_path)
    code = main(
        [
            "mock",
            "semantics",
            "--project",
            str(tmp_path),
            "--context",
            str(context_path),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    saved = json.loads(Path(payload["semantics"]).read_text(encoding="utf-8"))
    assert payload["deps"] == 1
    assert [dep["dep_id"] for dep in saved["deps"]] == ["inventory"]
    assert [branch["branch"] for branch in saved["deps"][0]["branches"]] == ["success"]


def test_mock_semantics_cli_merges_external_draft(tmp_path: Path, capsys) -> None:
    context_path = _context(tmp_path)
    draft = _write_json(
        tmp_path / "mock.draft.json",
        {
            "deps": [{"dep_id": "inventory", "branches": []}],
            "gaps": [{"dep_id": "inventory", "reason": "下游响应结构未定义"}],
            "questions": [],
        },
    )

    code = main(
        [
            "mock",
            "semantics",
            "--project",
            str(tmp_path),
            "--context",
            str(context_path),
            "--draft",
            str(draft),
            "--out",
            "mock_semantics.json",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    saved = json.loads((tmp_path / "mock_semantics.json").read_text(encoding="utf-8"))
    assert payload["gaps"] == 1
    assert saved["gaps"][0]["reason"] == "下游响应结构未定义"


def test_mock_semantics_cli_rejects_draft_and_llm_together(tmp_path: Path) -> None:
    context_path = _context(tmp_path)
    draft = _write_json(
        tmp_path / "mock.draft.json", {"deps": [], "gaps": [], "questions": []}
    )
    code = main(
        [
            "mock",
            "semantics",
            "--project",
            str(tmp_path),
            "--context",
            str(context_path),
            "--draft",
            str(draft),
            "--llm",
        ]
    )
    assert code == 1


def test_data_semantics_cli_runs_llm_stage(tmp_path: Path, monkeypatch, capsys) -> None:
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE inventory (id INTEGER PRIMARY KEY, status TEXT NOT NULL);"
        ),
    )
    catalog_path = _write_json(tmp_path / "data_catalog.json", catalog.model_dump(mode="json"))

    def fake_generate(catalog, *, llm, ruleset=None, context=None, prompt_path=None):
        draft = SemanticDraft(
            states=[
                DataState(
                    state_key="state_inventory_empty",
                    table_values={"inventory": [{"id": 1, "status": "empty"}]},
                    rule_keys=[],
                )
            ]
        )
        applied = apply_semantics(catalog.model_copy(deep=True), draft, ruleset=ruleset)
        return draft, applied

    class _DummyLLM:
        def __init__(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(
        "astsight.agent.data_prep.generate_data_semantics", fake_generate
    )
    monkeypatch.setattr("astsight.llm.load_config", lambda *_a, **_k: object())
    monkeypatch.setattr("astsight.llm.LLMClient", _DummyLLM)

    code = main(
        [
            "data",
            "semantics",
            "--project",
            str(tmp_path),
            "--catalog",
            str(catalog_path),
            "--llm",
            "--out",
            str(catalog_path),
        ]
    )

    assert code == 0
    json.loads(capsys.readouterr().out)
    saved = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert [state["state_key"] for state in saved["states"]] == [
        "state_inventory_empty"
    ]


def test_data_semantics_cli_requires_draft_or_llm(tmp_path: Path) -> None:
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl("CREATE TABLE inventory (id INTEGER PRIMARY KEY);"),
    )
    catalog_path = _write_json(tmp_path / "data_catalog.json", catalog.model_dump(mode="json"))
    code = main(
        [
            "data",
            "semantics",
            "--project",
            str(tmp_path),
            "--catalog",
            str(catalog_path),
        ]
    )
    assert code == 1
