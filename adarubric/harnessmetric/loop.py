"""Explicit generate -> execute -> measure -> refine -> re-measure loop."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from adarubric.core.models import TaskDescription
from adarubric.harnessmetric.codebuddy import (
    CodeBuddyResult,
    extract_json_object,
    run_codebuddy,
)
from adarubric.harnessmetric.generator import generate_operational_rubric
from adarubric.harnessmetric.models import (
    HarnessMetricResult,
    LoopIteration,
    MeasurementBatch,
    MetricClass,
    MetricMeasurement,
    MetricStatus,
    OperationalRubric,
    Usage,
)


def _run(command: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _atomic_model_write(path: Path, model: HarnessMetricResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class HarnessMetricLoop:
    """Control a frozen CodeBuddy executor with measured rubric bottlenecks."""

    def __init__(
        self,
        *,
        task: TaskDescription,
        workspace: Path,
        artifact_root: Path,
        original_prompt: str,
        repository_context: str,
        model: str,
        effort: str = "medium",
        initial_metric_policy: str = "off",
        verifier_enabled: bool = True,
        agent_timeout_seconds: int = 7200,
        generator_timeout_seconds: int = 7200,
        verifier_timeout_seconds: int = 7200,
        max_refinements: int = 12,
        max_loop_seconds: int = 43200,
    ) -> None:
        if initial_metric_policy not in {"off", "hard", "all"}:
            raise ValueError("initial_metric_policy must be off, hard, or all")
        self.task = task
        self.workspace = workspace.resolve()
        self.artifact_root = artifact_root.resolve()
        self.original_prompt = original_prompt
        self.repository_context = repository_context
        self.model = model
        self.effort = effort
        self.initial_metric_policy = initial_metric_policy
        self.verifier_enabled = verifier_enabled
        self.agent_timeout_seconds = agent_timeout_seconds
        self.generator_timeout_seconds = generator_timeout_seconds
        self.verifier_timeout_seconds = verifier_timeout_seconds
        self.max_refinements = max_refinements
        self.max_loop_seconds = max_loop_seconds
        digest = hashlib.sha256(
            f"{task.task_id}:{model}:{self.artifact_root}".encode()
        ).hexdigest()[:16]
        self.session_id = f"hm-{digest}"
        self.state_path = self.artifact_root / "harnessmetric_result.json"

    def _load_or_generate_contract(self) -> tuple[OperationalRubric, Usage]:
        metric_root = self.artifact_root / "metric_generation"
        path = metric_root / "operational_rubric.json"
        if path.is_file():
            rubric = OperationalRubric.model_validate_json(path.read_text(encoding="utf-8"))
            usage_path = metric_root / "usage.json"
            usage = (
                Usage.model_validate_json(usage_path.read_text(encoding="utf-8"))
                if usage_path.is_file()
                else Usage()
            )
            return rubric, usage
        return generate_operational_rubric(
            task=self.task,
            repository_context=self.repository_context,
            workspace=self.workspace,
            artifact_root=metric_root,
            model=self.model,
            effort=self.effort,
            timeout_seconds=self.generator_timeout_seconds,
        )

    def _initial_prompt(self, rubric: OperationalRubric) -> str:
        if self.initial_metric_policy == "off":
            return self.original_prompt
        return (
            self.original_prompt
            + "\n\n"
            + rubric.to_executor_prompt(include_exploratory=self.initial_metric_policy == "all")
        )

    def _agent_call(
        self,
        *,
        prompt: str,
        phase: str,
        index: int,
        resume_session_id: str | None,
    ) -> CodeBuddyResult:
        root = self.artifact_root / "executor" / f"{index:02d}_{phase}"
        return run_codebuddy(
            workspace=self.workspace,
            prompt=prompt,
            event_log=root / "events.json",
            stderr_log=root / "stderr.log",
            model=self.model,
            effort=self.effort,
            timeout_seconds=self.agent_timeout_seconds,
            tools="default",
            session_id=self.session_id if resume_session_id is None else None,
            resume_session_id=resume_session_id,
            persist_session=True,
            # No max-turn limit: the agent ends naturally or is budget-censored by
            # the generous wall-time watchdog.
            max_turns=None,
        )

    def _verification_workspace(self, index: int) -> Path:
        if not (self.workspace / ".git" / "index").is_file():
            restored = _run(["git", "reset", "--mixed", "HEAD"], self.workspace)
            if restored.returncode != 0:
                raise RuntimeError(f"could not restore missing Git index: {restored.stderr}")
        # Keep the checkout path short: several SWE-bench repositories contain
        # tracked paths close to Windows' legacy MAX_PATH limit. Verifier logs
        # remain under ``verifier/`` while this disposable checkout uses ``v/``.
        checkout_root = self.artifact_root / "v" / f"{index:02d}"
        checkout_root.mkdir(parents=True, exist_ok=True)
        root: Path | None = None
        for slot in range(32):
            candidate = checkout_root / ("w" if slot == 0 else f"x{slot:02d}")
            if candidate.exists():
                try:
                    shutil.rmtree(candidate)
                except OSError:
                    # A timed-out CodeBuddy child or virus scanner can retain a
                    # Windows handle briefly. Use a fresh isolated checkout
                    # instead of turning a verifier timeout into task failure.
                    continue
            root = candidate
            break
        if root is None:
            raise RuntimeError("all verifier checkout slots are still locked")
        cloned = _run(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "clone",
                "--shared",
                str(self.workspace),
                str(root),
            ],
            self.artifact_root,
        )
        if cloned.returncode != 0:
            raise RuntimeError(f"could not clone verifier workspace: {cloned.stderr}")
        configured = _run(["git", "config", "core.longpaths", "true"], root)
        if configured.returncode != 0:
            raise RuntimeError(
                f"could not enable long paths in verifier workspace: {configured.stderr}"
            )
        patch = _run(["git", "diff", "--binary", "HEAD", "--", "."], self.workspace)
        if patch.returncode != 0:
            raise RuntimeError(f"could not capture executor patch: {patch.stderr}")
        if patch.stdout.strip():
            git = shutil.which("git.exe") or shutil.which("git")
            if git is None:
                raise RuntimeError("git was not found while creating verifier workspace")
            applied = subprocess.run(  # noqa: S603
                [git, "apply", "--whitespace=nowarn", "-"],
                cwd=root,
                input=patch.stdout,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if applied.returncode != 0:
                raise RuntimeError(f"could not apply executor patch for verifier: {applied.stderr}")
        untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], self.workspace)
        for relative in untracked.stdout.splitlines():
            source = self.workspace / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, target)
        return root

    def _normalize_measurement(self, raw: object, rubric: OperationalRubric) -> MeasurementBatch:
        batch = MeasurementBatch.model_validate(raw)
        if batch.task_id != self.task.task_id:
            batch = batch.model_copy(update={"task_id": self.task.task_id})
        supplied = batch.by_name()
        normalized: list[MetricMeasurement] = []
        for metric in rubric.metrics:
            found = supplied.get(metric.name)
            if found is None:
                found = MetricMeasurement(
                    name=metric.name,
                    status=MetricStatus.BLOCKED,
                    evidence=[],
                    confidence=0.0,
                    next_action="Verifier omitted this metric; re-measure it.",
                )
            normalized.append(found)
        return batch.model_copy(update={"measurements": normalized})

    def _measure(self, rubric: OperationalRubric, index: int) -> tuple[MeasurementBatch, Usage]:
        verify_workspace = self._verification_workspace(index)
        schema = MeasurementBatch.model_json_schema()
        prompt = f"""You are the isolated HarnessMetric verifier. Do not implement or
edit the solution. Inspect the current repository state and run only relevant public
tests/probes needed to measure every metric. The original issue is authoritative.
Exploratory probes may expose risk but cannot create new requirements. Never seek hidden
tests, gold patches, benchmark metadata, Docker image internals, or files outside this
repository. Ground every status in concrete command/file evidence. Recommend stopping
only when every hard requirement and regression constraint is satisfied and a relevant
project test command passed.

Task:\n{self.task.instruction}

Operational rubric:\n{rubric.model_dump_json(indent=2)}

Return one raw JSON object matching this schema:\n{json.dumps(schema, ensure_ascii=False)}
"""
        total = Usage()
        last_error: Exception | None = None
        for attempt in range(1, 3):
            attempt_prompt = prompt
            if last_error is not None:
                attempt_prompt += (
                    "\nYour prior result was invalid. Return a complete replacement: "
                    + str(last_error)[:1500]
                )
            result = run_codebuddy(
                workspace=verify_workspace,
                prompt=attempt_prompt,
                event_log=(
                    self.artifact_root
                    / "verifier"
                    / f"{index:02d}"
                    / f"events_attempt_{attempt}.json"
                ),
                stderr_log=(
                    self.artifact_root
                    / "verifier"
                    / f"{index:02d}"
                    / f"stderr_attempt_{attempt}.log"
                ),
                model=self.model,
                effort=self.effort,
                timeout_seconds=self.verifier_timeout_seconds,
                tools="default",
                persist_session=False,
                max_turns=None,
            )
            total = total + result.usage
            if result.infrastructure_error is not None:
                raise RuntimeError(result.infrastructure_error)
            if result.return_code != 0:
                last_error = RuntimeError(f"verifier exited {result.return_code}")
                continue
            try:
                batch = self._normalize_measurement(
                    extract_json_object(result.final_message), rubric
                )
                path = self.artifact_root / "verifier" / f"{index:02d}" / "measurement.json"
                path.write_text(batch.model_dump_json(indent=2) + "\n", encoding="utf-8")
                return batch, total
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
        raise RuntimeError(f"verifier returned no valid measurement: {last_error}")

    @staticmethod
    def _can_stop(batch: MeasurementBatch, rubric: OperationalRubric) -> bool:
        by_name = batch.by_name()
        required_satisfied = all(
            by_name.get(name) is not None
            and by_name[name].status == MetricStatus.SATISFIED
            and by_name[name].scope_risk is None
            for name in rubric.required_metric_names
        )
        return batch.recommended_stop and required_satisfied

    @staticmethod
    def _feedback(batch: MeasurementBatch, rubric: OperationalRubric) -> str:
        definitions = {metric.name: metric for metric in rubric.metrics}
        bottlenecks = [
            measurement
            for measurement in batch.measurements
            if definitions[measurement.name].metric_class != MetricClass.EXPLORATORY_PROBE
            and measurement.status != MetricStatus.SATISFIED
        ]
        bottlenecks.sort(key=lambda item: definitions[item.name].weight, reverse=True)
        lines = [
            "Continue implementing the original issue. The isolated verifier measured the",
            "following unsatisfied task-anchored metrics. Fix these bottlenecks, run the",
            "related public tests, and leave the repository ready for submission. Do not",
            "broaden behavior to satisfy exploratory guesses.",
        ]
        if not bottlenecks:
            lines.append(
                "No required metric is clearly unsatisfied; run the relevant project tests."
            )
        for measurement in bottlenecks[:3]:
            metric = definitions[measurement.name]
            lines.extend(
                [
                    f"\n- {metric.name}: {measurement.status.value}",
                    f"  Target: {metric.target}",
                    f"  Evidence: {'; '.join(measurement.evidence) or measurement.observed_value}",
                    f"  Next action: {measurement.next_action}",
                    f"  Scope guard: {metric.scope_guard}",
                ]
            )
        lines.append(f"\nVerifier test status: {batch.project_test_status}")
        return "\n".join(lines)

    def run(self) -> HarnessMetricResult:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        rubric, generator_usage = self._load_or_generate_contract()
        if self.state_path.is_file():
            result = HarnessMetricResult.model_validate_json(
                self.state_path.read_text(encoding="utf-8")
            )
            if result.stopped or result.budget_censored:
                return result
            result.contract = rubric
        else:
            result = HarnessMetricResult(
                task_id=self.task.task_id,
                model=self.model,
                initial_metric_policy=self.initial_metric_policy,
                contract=rubric,
                generator_usage=generator_usage,
                session_id=self.session_id,
            )
            _atomic_model_write(self.state_path, result)
        loop_started = time.perf_counter()

        if not any(item.phase == "initial_execution" for item in result.iterations):
            # In prompt-engineering mode (verifier disabled) the generated
            # metrics must reach the executor even when the default policy is
            # "off" -- the metrics ARE the whole intervention.
            if not self.verifier_enabled:
                initial_prompt = (
                    self.original_prompt
                    + "\n\n"
                    + rubric.to_executor_prompt(include_exploratory=True)
                )
            else:
                initial_prompt = self._initial_prompt(rubric)
            initial = self._agent_call(
                prompt=initial_prompt,
                phase="initial",
                index=0,
                resume_session_id=None,
            )
            if initial.infrastructure_error is not None:
                raise RuntimeError(initial.infrastructure_error)
            result.executor_usage = result.executor_usage + initial.usage
            result.session_id = initial.session_id
            result.iterations.append(
                LoopIteration(
                    index=0,
                    phase="initial_execution",
                    usage=initial.usage,
                    session_id=initial.session_id,
                    termination_reason=initial.termination_reason,
                )
            )
            if initial.termination_reason is not None:
                result.budget_censored = True
                result.stop_reason = initial.termination_reason
                _atomic_model_write(self.state_path, result)
                return result
            if initial.return_code != 0:
                _atomic_model_write(self.state_path, result)
                raise RuntimeError(f"initial executor exited {initial.return_code}")
            _atomic_model_write(self.state_path, result)

        if not self.verifier_enabled:
            # Ablation: metrics guide the executor's prompt only; no verifier
            # measurement/refine loop. Stop after the single implementation pass.
            result.stopped = True
            result.stop_reason = "prompt_engineering_only"
            _atomic_model_write(self.state_path, result)
            return result

        measurement_count = sum(item.phase == "measurement" for item in result.iterations)
        last = result.iterations[-1]
        if last.phase == "measurement" and last.measurement is not None:
            if self._can_stop(last.measurement, rubric):
                result.stopped = True
                result.stop_reason = last.measurement.stop_reason
                _atomic_model_write(self.state_path, result)
                return result
            if last.index >= self.max_refinements:
                result.budget_censored = True
                result.stop_reason = "refinement_budget"
                _atomic_model_write(self.state_path, result)
                return result
            feedback = last.feedback or self._feedback(last.measurement, rubric)
            last.feedback = feedback
            _atomic_model_write(self.state_path, result)
            resumed = self._agent_call(
                prompt=feedback,
                phase="refine",
                index=last.index + 1,
                resume_session_id=result.session_id,
            )
            if resumed.infrastructure_error is not None:
                raise RuntimeError(resumed.infrastructure_error)
            result.executor_usage = result.executor_usage + resumed.usage
            result.session_id = resumed.session_id or result.session_id
            result.iterations.append(
                LoopIteration(
                    index=last.index + 1,
                    phase="refinement",
                    feedback=feedback,
                    usage=resumed.usage,
                    session_id=resumed.session_id,
                    termination_reason=resumed.termination_reason,
                )
            )
            if resumed.termination_reason is not None:
                result.budget_censored = True
                result.stop_reason = resumed.termination_reason
                _atomic_model_write(self.state_path, result)
                return result
            if resumed.return_code != 0:
                _atomic_model_write(self.state_path, result)
                raise RuntimeError(f"refinement executor exited {resumed.return_code}")
            _atomic_model_write(self.state_path, result)

        for index in range(measurement_count, self.max_refinements + 1):
            if time.perf_counter() - loop_started > self.max_loop_seconds:
                result.budget_censored = True
                result.stop_reason = "loop_wall_time"
                break
            batch, verifier_usage = self._measure(rubric, index)
            result.verifier_usage = result.verifier_usage + verifier_usage
            measurement_iteration = LoopIteration(
                index=index,
                phase="measurement",
                measurement=batch,
                usage=verifier_usage,
            )
            result.iterations.append(measurement_iteration)
            _atomic_model_write(self.state_path, result)
            if self._can_stop(batch, rubric):
                result.stopped = True
                result.stop_reason = batch.stop_reason
                break
            if index >= self.max_refinements:
                result.budget_censored = True
                result.stop_reason = "refinement_budget"
                break
            feedback = self._feedback(batch, rubric)
            measurement_iteration.feedback = feedback
            resumed = self._agent_call(
                prompt=feedback,
                phase="refine",
                index=index + 1,
                resume_session_id=result.session_id,
            )
            if resumed.infrastructure_error is not None:
                raise RuntimeError(resumed.infrastructure_error)
            result.executor_usage = result.executor_usage + resumed.usage
            result.session_id = resumed.session_id or result.session_id
            result.iterations.append(
                LoopIteration(
                    index=index + 1,
                    phase="refinement",
                    feedback=feedback,
                    usage=resumed.usage,
                    session_id=resumed.session_id,
                    termination_reason=resumed.termination_reason,
                )
            )
            if resumed.termination_reason is not None:
                result.budget_censored = True
                result.stop_reason = resumed.termination_reason
                break
            if resumed.return_code != 0:
                _atomic_model_write(self.state_path, result)
                raise RuntimeError(f"refinement executor exited {resumed.return_code}")
            _atomic_model_write(self.state_path, result)

        _atomic_model_write(self.state_path, result)
        return result
