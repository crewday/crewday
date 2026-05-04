"""Focused tests for ``scripts/agent-code-health.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_code_health() -> ModuleType:
    module_name = "agent_code_health"
    script = Path(__file__).resolve().parents[3] / "scripts" / "agent-code-health.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_parses_lizard_csv_and_duplicate_blocks() -> None:
    code_health = load_code_health()

    funcs = code_health.parse_csv(
        "\n".join(
            [
                "not,csv",
                "61,16,120,7,70,12-80,app/domain/tasks.py,create_task,"
                "create_task,12,80",
            ]
        )
    )
    blocks, dup_rate = code_health.parse_dup(
        "\n".join(
            [
                "Duplicate block:",
                "app/a.py:10 ~ 20",
                "app/b.py:30~40@beta",
                "Total duplicate rate: 4.2%",
            ]
        )
    )

    assert len(funcs) == 1
    assert funcs[0].ccn == 16
    assert funcs[0].nloc == 61
    assert funcs[0].params == 7
    assert blocks == [["app/a.py:10 ~ 20", "app/b.py:30~40@beta"]]
    assert dup_rate == "4.2%"
    assert code_health.parse_duplicate_location(blocks[0][0]).start == 10
    assert code_health.parse_duplicate_location(blocks[0][0]).end == 20


def test_json_report_keeps_all_metric_and_duplicate_findings(
    tmp_path: Path,
) -> None:
    code_health = load_code_health()
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "def tangled(a, b, c, d, e, f, g):",
                "    # code-health: ignore[ccn, duplicate] Deliberate parity matrix.",
                "    return a or b or c or d or e or f or g",
                "",
            ]
        ),
        encoding="utf-8",
    )
    funcs = [
        code_health.Func(
            nloc=61,
            ccn=20,
            tokens=120,
            params=7,
            length=63,
            file=str(source),
            name="tangled",
            start=1,
            end=3,
        )
    ]

    suppressions, invalid = code_health.discover_suppressions({str(source)})
    metric_findings = {
        "ccn": code_health.build_metric_findings(
            funcs,
            category="ccn",
            key=lambda func: func.ccn,
            threshold=15,
            suppressions=suppressions,
        ),
        "nloc": code_health.build_metric_findings(
            funcs,
            category="nloc",
            key=lambda func: func.nloc,
            threshold=60,
            suppressions=suppressions,
        ),
        "params": code_health.build_metric_findings(
            funcs,
            category="params",
            key=lambda func: func.params,
            threshold=6,
            suppressions=suppressions,
        ),
    }
    duplicate_findings = [
        code_health.build_duplicate_finding(
            [
                f"{source}:1~3@sample",
                f"{source}:20~22@sample_copy",
            ],
            suppressions,
        )
    ]
    report = code_health.build_report(
        paths=[str(source)],
        funcs=funcs,
        metric_findings=metric_findings,
        duplicate_findings=duplicate_findings,
        thresholds={"ccn": 15, "nloc": 60, "params": 6},
        suppressions=suppressions,
        invalid_suppressions=invalid,
        duplicate_rates=["py=10.0%"],
    )

    assert invalid == []
    assert len(report["findings"]) == 4
    suppressed = {
        finding["category"]: finding
        for finding in report["findings"]
        if finding["suppressed"]
    }
    assert suppressed["ccn"]["suppression_reason"] == "Deliberate parity matrix."
    assert suppressed["duplicate"]["suppression_target"] == f"{source}:2"
    assert report["summary"]["nloc"]["unsuppressed"] == 1
    assert report["summary"]["params"]["unsuppressed"] == 1
    assert report["summary"]["duplicate"]["suppressed"] == 1
    assert report["duplicate_blocks"][0]["locations"][1]["start"] == 20


def test_suppressions_require_reason(tmp_path: Path) -> None:
    code_health = load_code_health()
    source = tmp_path / "sample.py"
    source.write_text(
        "def tangled():\n    # code-health: ignore[ccn]\n    return True\n",
        encoding="utf-8",
    )

    suppressions, invalid = code_health.discover_suppressions({str(source)})

    assert suppressions == {}
    assert len(invalid) == 1
    assert invalid[0].problem == "expected code-health: ignore[category] reason"
