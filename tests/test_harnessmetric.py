"""Unit tests for operational metric control and the shared run ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adarubric.harnessmetric.ledger import RunLedger
from adarubric.harnessmetric.loop import HarnessMetricLoop
from adarubric.harnessmetric.models import (
    MeasurementBatch,
    MetricClass,
    MetricMeasurement,
    MetricStatus,
    OperationalMetric,
    OperationalRubric,
    Usage,
)


def metric(name: str, metric_class: MetricClass) -> OperationalMetric:
    return OperationalMetric(
        name=name,
        description=f"Measure the concrete behavior required by {name} in the visible project.",
        metric_class=metric_class,
        source_anchor=f"Issue text explicitly anchors {name}",
        measurement=f"Run the focused public test for {name}",
        target="Focused test passes",
        scope_guard="Do not change behavior beyond the issue text.",
        anti_gaming_check="Run a neighboring regression test as well.",
    )


@pytest.fixture
def operational_rubric() -> OperationalRubric:
    return OperationalRubric(
        task_id="task-1",
        task_summary="Implement the requested behavior without regressions.",
        metrics=[
            metric("RequiredBehavior", MetricClass.HARD_REQUIREMENT),
            metric("RegressionSafety", MetricClass.REGRESSION_CONSTRAINT),
            metric("BoundaryProbe", MetricClass.EXPLORATORY_PROBE),
        ],
        stop_condition="All required metrics and relevant public tests pass.",
        generation_rationale="The dimensions isolate behavior, regression risk, and exploration.",
    )


def test_operational_rubric_rejects_unknown_dependency() -> None:
    payload = {
        "task_id": "task-1",
        "task_summary": "A sufficiently detailed task summary.",
        "metrics": [
            metric("One", MetricClass.HARD_REQUIREMENT).model_copy(
                update={"dependencies": ["Missing"]}
            ),
            metric("Two", MetricClass.REGRESSION_CONSTRAINT),
            metric("Three", MetricClass.EXPLORATORY_PROBE),
        ],
        "stop_condition": "All task-anchored requirements are satisfied.",
        "generation_rationale": "Three orthogonal dimensions are needed for this task.",
    }
    with pytest.raises(ValidationError, match="unknown dependencies"):
        OperationalRubric.model_validate(payload)


def test_exploratory_probe_does_not_block_stop(
    operational_rubric: OperationalRubric,
) -> None:
    batch = MeasurementBatch(
        task_id="task-1",
        measurements=[
            MetricMeasurement(name="RequiredBehavior", status=MetricStatus.SATISFIED),
            MetricMeasurement(name="RegressionSafety", status=MetricStatus.SATISFIED),
            MetricMeasurement(name="BoundaryProbe", status=MetricStatus.UNSATISFIED),
        ],
        project_test_status="focused and regression tests passed",
        recommended_stop=True,
        stop_reason="All task-anchored metrics pass.",
    )
    assert HarnessMetricLoop._can_stop(batch, operational_rubric)


def test_scope_risk_blocks_stop(operational_rubric: OperationalRubric) -> None:
    batch = MeasurementBatch(
        task_id="task-1",
        measurements=[
            MetricMeasurement(
                name="RequiredBehavior",
                status=MetricStatus.SATISFIED,
                scope_risk="Implementation rejects inputs not covered by the issue.",
            ),
            MetricMeasurement(name="RegressionSafety", status=MetricStatus.SATISFIED),
            MetricMeasurement(name="BoundaryProbe", status=MetricStatus.SATISFIED),
        ],
        project_test_status="tests passed",
        recommended_stop=True,
        stop_reason="Verifier proposed stopping.",
    )
    assert not HarnessMetricLoop._can_stop(batch, operational_rubric)


def test_usage_addition() -> None:
    assert Usage(input_tokens=2, output_tokens=3) + Usage(input_tokens=5, output_tokens=7) == Usage(
        input_tokens=7, output_tokens=10
    )


def _arm_result(resolved: bool, wall: float) -> dict[str, object]:
    return {
        "status": "completed",
        "resolved": resolved,
        "usage": {
            "wall_seconds": wall,
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 20,
            "output_tokens": 4,
            "turns": 3,
        },
        "official_score": {"completed": True, "resolved": resolved},
    }


def test_ledger_records_mismatch_and_time_explanation(tmp_path: Path) -> None:
    path = tmp_path / "experiment.json"
    ledger = RunLedger(path)
    ledger.initialize(
        benchmark="SWE-bench Verified",
        manifest="manifest.json",
        task_count=1,
        initial_metric_policy="off",
    )
    task = {
        "instance_id": "repo__repo-1",
        "repo": "repo/repo",
        "base_commit": "abc",
        "problem_statement": "Fix the issue.",
    }
    ledger.record_task(
        arm="plain_deepseek_v4_flash",
        task=task,
        arm_result=_arm_result(True, 100.0),
    )
    ledger.record_task(
        arm="harnessmetric_deepseek_v4_flash",
        task=task,
        arm_result=_arm_result(False, 900.0),
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    comparison = data["comparisons"]["repo__repo-1"]["deepseek-v4-flash"]
    assert comparison["success_mismatch_explanation"]["winner"] == "plain"
    assert comparison["large_time_difference"]["slower"] == "harnessmetric"
