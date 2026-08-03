"""Typed artifacts for online, verifier-backed metric control."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class MetricClass(str, Enum):
    """How strongly a generated metric may constrain the implementation."""

    HARD_REQUIREMENT = "hard_requirement"
    REGRESSION_CONSTRAINT = "regression_constraint"
    EXPLORATORY_PROBE = "exploratory_probe"


class MetricStatus(str, Enum):
    UNKNOWN = "unknown"
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    BLOCKED = "blocked"
    INVALID = "invalid"


class OperationalMetric(BaseModel):
    """A task-specific rubric dimension compiled into an online measurement."""

    name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=12)
    weight: float = Field(default=1.0, gt=0.0, le=2.0)
    metric_class: MetricClass
    source_anchor: str = Field(
        min_length=3,
        description="Task quote or visible repository evidence supporting this metric.",
    )
    measurement: str = Field(
        min_length=12,
        description="A concrete, repeatable procedure using only visible repository evidence.",
    )
    target: str = Field(min_length=3)
    scope_guard: str = Field(
        min_length=8,
        description="What must not be promoted into a new task requirement.",
    )
    anti_gaming_check: str = Field(min_length=8)
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("source_anchor")
    @classmethod
    def _hard_metrics_need_non_generic_anchor(cls, value: str) -> str:
        if value.strip().casefold() in {"task", "issue", "repository", "none", "n/a"}:
            raise ValueError("source_anchor must identify concrete visible evidence")
        return value


class OperationalRubric(BaseModel):
    """AdaRubric-style adaptive dimensions with operational measurement semantics."""

    task_id: str
    task_summary: str = Field(min_length=12)
    metrics: list[OperationalMetric] = Field(min_length=3, max_length=7)
    stop_condition: str = Field(min_length=12)
    generation_rationale: str = Field(min_length=12)

    @model_validator(mode="after")
    def _validate_graph(self) -> OperationalRubric:
        folded = [metric.name.casefold() for metric in self.metrics]
        if len(folded) != len(set(folded)):
            raise ValueError("metric names must be unique")
        names = set(folded)
        for metric in self.metrics:
            unknown = [dep for dep in metric.dependencies if dep.casefold() not in names]
            if unknown:
                raise ValueError(f"{metric.name} has unknown dependencies: {unknown}")
            if metric.name.casefold() in {dep.casefold() for dep in metric.dependencies}:
                raise ValueError(f"{metric.name} cannot depend on itself")
        return self

    @property
    def required_metric_names(self) -> list[str]:
        return [
            metric.name
            for metric in self.metrics
            if metric.metric_class != MetricClass.EXPLORATORY_PROBE
        ]

    def to_executor_prompt(self, *, include_exploratory: bool = False) -> str:
        metrics = [
            metric
            for metric in self.metrics
            if include_exploratory or metric.metric_class != MetricClass.EXPLORATORY_PROBE
        ]
        lines = ["Operational metrics (the original issue remains authoritative):"]
        for metric in metrics:
            lines.extend(
                [
                    f"- {metric.name} [{metric.metric_class.value}] target={metric.target}",
                    f"  Evidence anchor: {metric.source_anchor}",
                    f"  Measure with: {metric.measurement}",
                    f"  Scope guard: {metric.scope_guard}",
                    f"  Anti-gaming: {metric.anti_gaming_check}",
                ]
            )
        lines.append(f"Stop condition: {self.stop_condition}")
        return "\n".join(lines)


class MetricMeasurement(BaseModel):
    name: str
    status: MetricStatus
    observed_value: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    next_action: str = ""
    scope_risk: str | None = None


class MeasurementBatch(BaseModel):
    task_id: str
    measurements: list[MetricMeasurement]
    project_test_status: str
    recommended_stop: bool = False
    stop_reason: str

    def by_name(self) -> dict[str, MetricMeasurement]:
        return {measurement.name: measurement for measurement in self.measurements}


class Usage(BaseModel):
    wall_seconds: float = 0.0
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            wall_seconds=self.wall_seconds + other.wall_seconds,
            input_tokens=self.input_tokens + other.input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            turns=self.turns + other.turns,
        )


class LoopIteration(BaseModel):
    index: int
    phase: str
    measurement: MeasurementBatch | None = None
    feedback: str | None = None
    usage: Usage = Field(default_factory=Usage)
    session_id: str | None = None
    termination_reason: str | None = None


class HarnessMetricResult(BaseModel):
    task_id: str
    model: str
    initial_metric_policy: str
    contract: OperationalRubric | None = None
    iterations: list[LoopIteration] = Field(default_factory=list)
    generator_usage: Usage = Field(default_factory=Usage)
    executor_usage: Usage = Field(default_factory=Usage)
    verifier_usage: Usage = Field(default_factory=Usage)
    stopped: bool = False
    stop_reason: str | None = None
    budget_censored: bool = False
    session_id: str | None = None

    @property
    def total_usage(self) -> Usage:
        return self.generator_usage + self.executor_usage + self.verifier_usage
