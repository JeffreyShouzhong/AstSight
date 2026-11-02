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
