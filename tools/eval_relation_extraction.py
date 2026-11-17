#!/usr/bin/env python3
"""离线评估 A1 对「显式公式关系」的抽取质量。

对评估集里的每条需求单独跑一次 A1，把模型输出的 `Rule.relations` 与标注对比，
输出关系召回/精度、表达式运算准确率与误抽（模型自己补公式）清单。

评估需要真实模型，默认不随测试跑：

    export ASTSIGHT_LLM_API_KEY=...
    python tools/eval_relation_extraction.py --llm-config engine/llm.json

没有配置模型时显式失败，不伪造 0 分或跳过结果。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
DEFAULT_CORPUS = ENGINE / "eval" / "relation_formulas.json"

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from astsight.agent.rule_analysis import generate_rule_draft  # noqa: E402
from astsight.evaluation import (  # noqa: E402
    aggregate,
    load_corpus,
    score_case,
    signatures,
)
from astsight.llm import LLMClient, LLMConfigError, load_config  # noqa: E402
from astsight.rules import build_rules_context  # noqa: E402
from astsight.rules.validate import validate_rule_draft  # noqa: E402


def _run_case(case: dict[str, Any], llm: LLMClient) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="astsight-eval-") as raw:
        project = Path(raw)
        doc = project / "requirements.md"
        doc.write_text(
            f"# 关系公式评估\n\n## {case['id']}\n\n{case['requirement']}\n",
            encoding="utf-8",
        )
        context = build_rules_context(project, [str(doc)])
        draft = generate_rule_draft(context, llm=llm)
        validation = validate_rule_draft(draft, context)
        predicted = signatures(draft.rules)
    result = score_case(case.get("expected") or [], predicted)
    result["id"] = case["id"]
    result["predicted_signatures"] = predicted
    result["validation_errors"] = list(validation.errors)
    result["validation_warnings"] = list(validation.warnings)
    return result


def _print_table(results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    print(
        f"{'case':<24} {'exp':>3} {'pred':>4} {'match':>5} {'strict':>6} "
        f"{'P':>6} {'R':>6} {'expr':>6}"
    )
    for result in results:
        print(
            f"{result['id']:<24} {result['expected']:>3} {result['predicted']:>4} "
            f"{result['matched']:>5} {result['strict']:>6} "
            f"{result['precision']:>6.2f} {result['recall']:>6.2f} "
            f"{result['expression_accuracy']:>6.2f}"
        )
    print("-" * 76)
    print(
        f"{'macro':<24} {summary['expected']:>3} {summary['predicted']:>4} "
        f"{summary['matched']:>5} {summary['strict']:>6} "
        f"{summary['precision']:>6.2f} {summary['recall']:>6.2f} "
        f"{summary['expression_accuracy']:>6.2f}"
    )
    for result in results:
        for item in result["missed"]:
            print(f"  MISS  {result['id']}: {item}")
        for item in result["unexpected"]:
            print(f"  EXTRA {result['id']}: {item}")
        for item in result["expression_mismatches"]:
            print(
                f"  EXPR  {result['id']}: expected "
                f"ops={item['expected'].get('ops')} "
                f"sources={item['expected'].get('sources')} "
                f"paths={item['expected'].get('paths')}; got "
                f"ops={item['predicted'].get('ops')} "
                f"sources={item['predicted'].get('sources')} "
                f"paths={item['predicted'].get('paths')}"
            )
        for error in result["validation_errors"]:
            print(f"  INVALID {result['id']}: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--llm-config", default=None)
    parser.add_argument("--out", default=None, help="把完整结果写成 JSON")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--max-cases", type=int, default=0)
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    cases = corpus["cases"]
    if args.max_cases > 0:
        cases = cases[: args.max_cases]
    try:
        llm = LLMClient(load_config(config_path=args.llm_config))
    except LLMConfigError as exc:
        print(
            f"缺少模型配置：{exc}\n"
            "请通过 --llm-config 或 ASTSIGHT_LLM_* 环境变量配置后再跑。",
            file=sys.stderr,
        )
        return 2

    results = [_run_case(case, llm) for case in cases]
    summary = aggregate((result["id"], result) for result in results)
    corpus_path = Path(args.corpus).resolve()
    try:
        corpus_display = corpus_path.relative_to(REPO).as_posix()
    except ValueError:
        corpus_display = str(corpus_path)
    payload = {
        "corpus": corpus_display,
        "name": corpus.get("name"),
        "llm": llm.config.redacted(),
        "summary": summary,
        "cases": results,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(results, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
