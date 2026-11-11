"""CLI 冒烟：规则 context 与 C5a 数据目录都能从命令行走通。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cli import main
from test_agent_flow import _copy_example


def test_rules_prepare_cli_writes_context_and_a1_prompt(
    tmp_path: Path, capsys
) -> None:
    project = _copy_example(tmp_path)
    code = main(
        [
            "rules",
            "prepare",
            "--project",
            str(project),
            "--requirements",
            "docs/requirements.md",
            "--openapi",
            str(project / "openapi.json"),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    context_dir = Path(payload["context_dir"])
    assert (context_dir / "context.json").is_file()
    assert (context_dir / "rules.schema.json").is_file()
    prompt = (context_dir / "prompt.md").read_text(encoding="utf-8")
    assert "A1" in prompt
    assert "source.quote" in prompt


def _extract_draft(user: str) -> dict:
    """模拟模型：从上下文视图里选一个 section，产出带原文引用的草案。"""
    context = json.loads(user)["context"]
    document = next(
        doc
        for doc in context["documents"]
        if doc["doc"].endswith("requirements.md")
    )
    section = document["sections"][0]
    quote = section["text"].splitlines()[0].lstrip("- ").strip()
    return {
        "schema_version": 1,
        "context_id": context["context_id"],
        "rules": [
            {
                "text": "请求字段 sku 长度 3–40，quantity 为 1–99 的整数",
                "source": {
                    "doc": document["doc"],
                    "number": section["number"],
                    "quote": quote,
                },
                "authority": "A",
            }
        ],
        "questions": [
            {
                "id": "Q-01",
                "text": "价格服务超时返回什么？",
                "reason": "文档未定义",
                "source": "docs/requirements.md#2",
            }
        ],
    }


class _ExtractLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append({"system": system, "user": user})
        return _extract_draft(user)


def test_rules_extract_cli_is_the_requirements_entry(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _copy_example(tmp_path)
    llm = _ExtractLLM()
    monkeypatch.setattr("astsight.cli._rules_llm", lambda: llm)

    code = main(
        [
            "rules",
            "extract",
            "--project",
            str(project),
            "--requirements",
            "docs/requirements.md",
            "--openapi",
            str(project / "openapi.json"),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is True
    assert payload["business_rules"] == 1
    assert payload["contract_rules"] > 0
    assert payload["questions"] == 1
    assert Path(payload["rule_base"]) == project / "ruleset.json"
    assert Path(payload["review"]) == project / "ruleset.md"
    assert "## 2 规则表" in Path(payload["review"]).read_text(encoding="utf-8")
    context_dir = Path(payload["context_dir"])
    assert (context_dir / "rules.draft.json").is_file()
    # 模型收到的是确定性上下文视图，不是原始仓库路径。
    assert llm.calls and json.loads(llm.calls[0]["user"])["context"]["context_id"]


def test_rules_extract_dry_run_does_not_write_ruleset(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _copy_example(tmp_path)
    monkeypatch.setattr("astsight.cli._rules_llm", lambda: _ExtractLLM())

    code = main(
        [
            "rules",
            "extract",
            "--project",
            str(project),
            "--requirements",
            "docs/requirements.md",
            "--openapi",
            str(project / "openapi.json"),
            "--dry-run",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is False
    assert not (project / "ruleset.json").exists()
    assert not (project / "ruleset.md").exists()


def test_rules_c1_accepts_optional_design_and_openapi(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _copy_example(tmp_path)
    design = project / "docs" / "design.md"
    design.write_text("# 设计\n\n注册接口返回 200。\n", encoding="utf-8")
    # 明确验证“无 OpenAPI 也能完成 C1”，而不触发项目内自动发现。
    (project / "openapi.json").unlink()
    monkeypatch.setattr("astsight.cli._rules_llm", lambda: _ExtractLLM())

    code = main(
        [
            "rules",
            "c1",
            "--project",
            str(project),
            "--requirements",
            "docs/requirements.md",
            "--design",
            "docs/design.md",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is True
    assert payload["contract_rules"] == 0
    assert "openapi_missing" in payload["warnings"]
    assert (project / "ruleset.json").is_file()
    assert (project / "ruleset.md").is_file()

    context = json.loads(Path(payload["context"]).read_text(encoding="utf-8"))
    assert {document["doc"] for document in context["documents"]} == {
        "docs/requirements.md",
        "docs/design.md",
    }


def test_rules_import_cli_writes_review_markdown(tmp_path: Path, capsys) -> None:
    project = _copy_example(tmp_path)
    assert (
        main(
            [
                "rules",
                "prepare",
                "--project",
                str(project),
                "--requirements",
                "docs/requirements.md",
                "--openapi",
                str(project / "openapi.json"),
                "--json",
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    context = json.loads(
        (Path(prepared["context_dir"]) / "context.json").read_text(encoding="utf-8")
    )
    section = context["documents"][0]["sections"][0]
    quote = section["text"].splitlines()[0].lstrip("- ").strip()
    draft = {
        "schema_version": 1,
        "context_id": context["context_id"],
        "rules": [
            {
                "text": "请求字段 sku 长度 3–40，quantity 为 1–99 的整数",
                "source": {
                    "doc": context["documents"][0]["doc"],
                    "number": section["number"],
                    "quote": quote,
                },
                "authority": "A",
            }
        ],
        "questions": [
            {
                "id": "Q-01",
                "text": "价格服务超时返回什么？",
                "reason": "文档未定义",
                "source": "docs/requirements.md#2",
            }
        ],
    }
    draft_path = tmp_path / "rules.draft.json"
    draft_path.write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )

    code = main(
        [
            "rules",
            "import",
            "--project",
            str(project),
            "--context",
            prepared["context_dir"],
            "--draft",
            str(draft_path),
            "--rule-base",
            "ruleset.json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    review = Path(payload["review"])
    assert review == project / "ruleset.md"
    markdown = review.read_text(encoding="utf-8")
    assert "## 2 规则表" in markdown
    assert "## 6 业务确认清单" in markdown
    assert "价格服务超时返回什么？" in markdown


def test_cases_prepare_cli_scans_code_routes_without_openapi(
    tmp_path: Path, capsys
) -> None:
    project = _copy_example(tmp_path)
    (project / "openapi.json").unlink()

    code = main(
        [
            "cases",
            "prepare",
            "--project",
            str(project),
            "--requirements",
            "docs/requirements.md",
            "--codegraph",
            "off",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["openapi"] == ""
    assert "openapi_missing" in payload["warnings"]
    assert payload["endpoints"] >= 1
    context = json.loads(Path(payload["context"]).read_text(encoding="utf-8"))
    assert context["interface_evidence"]["routes"] == "code"
    assert all(
        profile["contract_incomplete"] for profile in context["profiles"]
    )


def test_data_catalog_cli_writes_catalog(tmp_path: Path, capsys) -> None:
    project = _copy_example(tmp_path)
    (project / "schema.sql").write_text(
        "CREATE TABLE products (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  sku TEXT NOT NULL UNIQUE,\n"
        "  stock INTEGER NOT NULL DEFAULT 0\n"
        ");\n",
        encoding="utf-8",
    )

    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(project),
            "--ddl",
            "schema.sql",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    catalog = json.loads(Path(payload["catalog"]).read_text(encoding="utf-8"))
    assert [table["name"] for table in catalog["tables"]] == ["products"]
    assert catalog["tables_order"] == ["products"]
    assert payload["tables"] == 1
