"""Generate AdaRubric dimensions that are safe to use as online controls."""

from __future__ import annotations

import json
from pathlib import Path

from adarubric.core.models import TaskDescription
from adarubric.harnessmetric.codebuddy import extract_json_object, run_agent
from adarubric.harnessmetric.models import OperationalRubric, Usage

SYSTEM_PROMPT = """You compile task-adaptive rubrics into operational metrics for a
frozen coding agent. You have no tools and MUST NOT emit or request tool calls. Use only
the task and repository context embedded in this prompt. Your first and only response
must be one raw JSON object with no Markdown fence, commentary, or tool-call markup.

Generate orthogonal, measurable dimensions, but do not invent requirements. Classify
each metric as:
- hard_requirement only when directly entailed by an exact task-text anchor;
- regression_constraint only when supported by visible repository behavior;
- exploratory_probe for plausible risks that may be measured but MUST NOT broaden the fix.

Every measurement must be repeatable with visible files or public tests. Hidden tests,
gold patches, benchmark metadata, and external repositories are unavailable. Include a
scope guard and an anti-gaming check for every metric. The original issue always has
priority over generated metrics. Prefer 3-5 non-redundant metrics.

VERIFIER DECISION (verifier_plan field): Decide whether verification is worth keeping
for this task and whether it should be deterministic:
- keep_verifier=false only when this task genuinely cannot be verified during the loop
  (e.g. the correct result is unknowable without hidden tests). Otherwise keep it true.
- rule_based=true when the task has an objective, checkable outcome: a numeric result,
  a fixed output format, a crash/error disappearing, an exact log line, a deterministic
  test suite, a known-good output file. Specify rule (plain language) and command (a
  concrete shell command using only visible files/public tests). When rule_based is
  true the deterministic rule replaces an open-ended LLM judge.
- rule_based=false when the outcome is inherently subjective or under-specified and
  only a human-style judgment can decide (rare for coding tasks).
"""


def generate_operational_rubric(
    *,
    task: TaskDescription,
    repository_context: str,
    workspace: Path,
    artifact_root: Path,
    model: str,
    effort: str = "medium",
    timeout_seconds: int = 1800,
    max_attempts: int = 3,
    runner: str = "codebuddy",
) -> tuple[OperationalRubric, Usage]:
    """Generate and locally validate a rubric without granting generator tools."""

    schema = OperationalRubric.model_json_schema()
    base_prompt = (
        f"{SYSTEM_PROMPT}\n\nReturn one raw JSON object matching this schema exactly. "
        "Do not inspect the workspace or call a tool; all allowed evidence is below:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Task ID: {task.task_id}\nTask instruction:\n{task.instruction}\n\n"
        f"Visible repository context:\n{repository_context}"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    total = Usage()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt
        if last_error is not None:
            prompt += (
                "\n\nThe previous response failed schema validation. Return a complete "
                f"replacement. Validation error: {str(last_error)[:2000]}"
            )
        result = run_agent(
            runner,
            workspace=workspace,
            prompt=prompt,
            event_log=artifact_root / f"events_attempt_{attempt}.json",
            stderr_log=artifact_root / f"stderr_attempt_{attempt}.log",
            model=model,
            effort=effort,
            timeout_seconds=timeout_seconds,
            tools="",
            persist_session=False,
        )
        total = total + result.usage
        if result.infrastructure_error is not None:
            raise RuntimeError(result.infrastructure_error)
        if result.return_code != 0:
            last_error = RuntimeError(f"CodeBuddy generator exited {result.return_code}")
            continue
        try:
            rubric = OperationalRubric.model_validate(extract_json_object(result.final_message))
            if rubric.task_id != task.task_id:
                rubric = rubric.model_copy(update={"task_id": task.task_id})
            (artifact_root / "operational_rubric.json").write_text(
                rubric.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            (artifact_root / "usage.json").write_text(
                total.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            return rubric, total
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    raise RuntimeError(
        f"No valid operational rubric after {max_attempts} attempts: {last_error}"
    ) from last_error
