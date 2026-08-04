"""Run one of four independent CodeBuddy x SWE-bench Verified arms."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from adarubric.core.models import TaskDescription
from adarubric.harnessmetric.codebuddy import run_codebuddy
from adarubric.harnessmetric.ledger import RunLedger
from adarubric.harnessmetric.loop import HarnessMetricLoop
from adarubric.harnessmetric.models import Usage
from adarubric.harnessmetric.swebench import (
    docker,
    grade,
    image_name,
    load_rows,
    model_patch,
    prepare_pristine,
    prepare_workspace,
    repository_context,
)

PROTOCOL = """Work autonomously on the issue below. Inspect and edit the repository,
run relevant tests, and leave a complete working implementation. Do not merely explain
the fix. Hidden tests, gold patches, and grading metadata are unavailable and must not
be sought. Preserve public API compatibility beyond the requested change. Use the
supplied .harnessmetric/run_tests.py wrapper and Python executable for Linux test
commands; do not inspect Docker image internals or files outside this repository. The
original issue is authoritative. Do not delete, move, rewrite, or otherwise modify
.git metadata; the harness needs the original Git index to extract your patch.
"""

HERE = Path(__file__).resolve().parent
TEST_HELPER = HERE / "run_tests_in_swebench.py"
WINDOWS_EVAL = HERE / "swebench_windows_eval.py"
ARM_DIRECTORIES = {
    "plain_deepseek_v4_flash": "pf",
    "plain_hy3": "ph",
    "harnessmetric_deepseek_v4_flash": "mf",
    "harnessmetric_hy3": "mh",
}
QUOTA_ERROR = "codebuddy_quota_exhausted"


def arm_name(treatment: str, model: str) -> str:
    suffix = "deepseek_v4_flash" if model == "deepseek-v4-flash" else "hy3"
    return f"{treatment}_{suffix}"


def _usage_dict(usage: Usage, *, end_to_end_wall: float) -> dict[str, Any]:
    payload = usage.model_dump()
    payload["agent_phase_wall_seconds"] = payload["wall_seconds"]
    payload["wall_seconds"] = end_to_end_wall
    return payload


def _failure_reason(score: dict[str, Any]) -> str | None:
    if score.get("resolved"):
        return None
    if score.get("empty_patch"):
        return "The executor produced no submission patch."
    if score.get("error"):
        return str(score["error"])
    report = score.get("official_report", {})
    tests = report.get("tests_status", {}) if isinstance(report, dict) else {}
    target = tests.get("FAIL_TO_PASS", {}).get("failure", [])
    regression = tests.get("PASS_TO_PASS", {}).get("failure", [])
    if target and regression:
        return f"Failed {len(target)} target tests and regressed {len(regression)} tests."
    if target:
        return f"Failed {len(target)} hidden target tests."
    if regression:
        return f"Regressed {len(regression)} previously passing tests."
    return "Official grader marked the patch unresolved."


def _plain(
    *,
    instance: dict[str, Any],
    workspace: Path,
    root: Path,
    prompt: str,
    model: str,
    effort: str,
    timeout: int,
) -> tuple[dict[str, Any], bool]:
    digest = hashlib.sha256(
        f"plain:{instance['instance_id']}:{model}:{root.resolve()}".encode()
    ).hexdigest()[:16]
    result = run_codebuddy(
        workspace=workspace,
        prompt=prompt,
        event_log=root / "executor" / "events.json",
        stderr_log=root / "executor" / "stderr.log",
        model=model,
        effort=effort,
        timeout_seconds=timeout,
        tools="default",
        session_id=f"plain-{digest}",
        persist_session=True,
        max_turns=None,
    )
    infrastructure_failure = result.infrastructure_error is not None or (
        result.return_code != 0 and result.termination_reason is None
    )
    payload = {
        "executor": {
            "return_code": result.return_code,
            "session_id": result.session_id,
            "termination_reason": result.termination_reason,
            "infrastructure_error": result.infrastructure_error,
            "final_message": result.final_message,
        },
        "phase_usage": {"executor": result.usage.model_dump()},
        "metrics": None,
        "iterations": [],
        "budget_censored": result.termination_reason is not None,
    }
    return payload, infrastructure_failure


def _harnessmetric(
    *,
    instance: dict[str, Any],
    workspace: Path,
    root: Path,
    prompt: str,
    model: str,
    effort: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], bool]:
    task = TaskDescription(
        task_id=instance["instance_id"],
        instruction=instance["problem_statement"],
        domain="software_issue_resolution",
        context={"repo": instance["repo"], "base_commit": instance["base_commit"]},
        expected_tools=["Read", "Edit", "Bash", "test_runner"],
    )
    loop = HarnessMetricLoop(
        task=task,
        workspace=workspace,
        artifact_root=root,
        original_prompt=prompt,
        repository_context=repository_context(workspace, instance["problem_statement"]),
        model=model,
        effort=effort,
        initial_metric_policy=args.initial_metric_policy,
        agent_timeout_seconds=args.agent_timeout,
        generator_timeout_seconds=args.generator_timeout,
        verifier_timeout_seconds=args.verifier_timeout,
        max_refinements=args.max_refinements,
        max_loop_seconds=args.max_loop_hours * 3600,
    )
    result = loop.run()
    payload = {
        "executor": {
            "session_id": result.session_id,
            "termination_reason": result.stop_reason if result.budget_censored else None,
            "stopped": result.stopped,
            "stop_reason": result.stop_reason,
        },
        "phase_usage": {
            "generator": result.generator_usage.model_dump(),
            "executor": result.executor_usage.model_dump(),
            "verifier": result.verifier_usage.model_dump(),
        },
        "metrics": result.contract.model_dump(mode="json") if result.contract else None,
        "iterations": [item.model_dump(mode="json") for item in result.iterations],
        "budget_censored": result.budget_censored,
    }
    return payload, False


def run_instance(
    instance: dict[str, Any], args: argparse.Namespace, arm: str, ledger: RunLedger
) -> dict[str, Any]:
    short_root = args.run_root / ARM_DIRECTORIES[arm] / "i" / instance["instance_id"]
    legacy_root = args.run_root / arm / "instances" / instance["instance_id"]
    # Resume checkpoints written before the Windows path-shortening layout was
    # introduced. New instances always use the shorter path.
    root = legacy_root if legacy_root.exists() else short_root
    root.mkdir(parents=True, exist_ok=True)
    result_path = root / "result.json"
    if result_path.is_file():
        prior = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            raise ValueError(f"result root must be an object: {result_path}")
        typed_prior = cast(dict[str, Any], prior)
        if typed_prior.get("status") != "infrastructure_failure":
            ledger.record_task(arm=arm, task=instance, arm_result=typed_prior)
            return typed_prior

    started = time.perf_counter()
    image = image_name(instance["instance_id"])
    docker("pull", image, cwd=root, timeout=7200)
    pristine = prepare_pristine(instance, root, image)
    workspace = prepare_workspace(
        pristine=pristine,
        root=root / "agent",
        base_commit=instance["base_commit"],
        image=image,
        helper_script=TEST_HELPER,
    )
    wrapper = (
        f"& '{Path(sys.executable).resolve()}' .harnessmetric/run_tests.py "
        f"--image {image} <test command and arguments>"
    )
    prompt = f"{PROTOCOL}\nIssue:\n{instance['problem_statement']}\n\nTest wrapper:\n{wrapper}\n"
    (root / "prompt.txt").write_text(prompt, encoding="utf-8")

    if args.treatment == "plain":
        payload, infrastructure_failure = _plain(
            instance=instance,
            workspace=workspace,
            root=root,
            prompt=prompt,
            model=args.model,
            effort=args.effort,
            timeout=args.agent_timeout,
        )
        total_usage = Usage.model_validate(payload["phase_usage"]["executor"])
    else:
        payload, infrastructure_failure = _harnessmetric(
            instance=instance,
            workspace=workspace,
            root=root,
            prompt=prompt,
            model=args.model,
            effort=args.effort,
            args=args,
        )
        total_usage = sum(
            (Usage.model_validate(value) for value in payload["phase_usage"].values()),
            start=Usage(),
        )

    patch = model_patch(workspace, instance["base_commit"])
    (root / "model.patch").write_text(patch, encoding="utf-8")
    infrastructure_reason = payload.get("executor", {}).get("infrastructure_error")
    score = (
        {
            "completed": False,
            "resolved": False,
            "error": infrastructure_reason or "executor infrastructure failure",
        }
        if infrastructure_failure
        else grade(
            instance=instance,
            arm=arm,
            model=args.model,
            patch=patch,
            root=root,
            harness_python=args.harness_python,
            eval_script=WINDOWS_EVAL,
            timeout=args.grade_timeout,
        )
    )
    end_to_end = time.perf_counter() - started
    status = (
        "infrastructure_failure"
        if infrastructure_failure or not score.get("completed")
        else "censored"
        if payload["budget_censored"]
        else "completed"
    )
    record = {
        "status": status,
        "resolved": bool(score.get("resolved")),
        "empty_patch": not bool(patch.strip()),
        "failure_reason": infrastructure_reason or _failure_reason(score),
        "usage": _usage_dict(total_usage, end_to_end_wall=end_to_end),
        "official_score": score,
        **payload,
    }
    result_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ledger.record_task(arm=arm, task=instance, arm_result=record)

    if not args.keep_workspaces:
        for path in (workspace, pristine):
            if path.exists() and root.resolve() in path.resolve().parents:
                shutil.rmtree(path)
    docker("image", "rm", image, cwd=root, timeout=600, required=False)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rows-dir", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--harness-python", type=Path, required=True)
    parser.add_argument("--treatment", choices=("plain", "harnessmetric"), required=True)
    parser.add_argument("--model", choices=("deepseek-v4-flash", "hy3"), required=True)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--initial-metric-policy", choices=("off", "hard", "all"), default="off")
    parser.add_argument("--agent-timeout", type=int, default=7200)
    parser.add_argument("--generator-timeout", type=int, default=1800)
    parser.add_argument("--verifier-timeout", type=int, default=1800)
    parser.add_argument("--grade-timeout", type=int, default=1800)
    parser.add_argument("--max-refinements", type=int, default=12)
    parser.add_argument("--max-loop-hours", type=int, default=12)
    parser.add_argument("--infrastructure-retries", type=int, default=3)
    parser.add_argument("--quota-retry-seconds", type=int, default=300)
    parser.add_argument("--quota-max-wait-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--instance-id", action="append")
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = manifest["instances"]
    if args.instance_id:
        requested = set(args.instance_id)
        selected = [item for item in selected if item["instance_id"] in requested]
    selected = selected[: args.limit]
    rows = load_rows(args.rows_dir)
    instances = []
    for metadata in selected:
        instance = dict(rows[metadata["instance_id"]])
        instance["difficulty"] = metadata.get("difficulty")
        instances.append(instance)

    arm = arm_name(args.treatment, args.model)
    args.run_root.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(args.ledger)
    ledger.initialize(
        benchmark="SWE-bench Verified",
        manifest=str(args.manifest.resolve()),
        task_count=len(instances),
        initial_metric_policy=args.initial_metric_policy,
    )
    ledger.mark_arm(arm, status="running")
    failures_path = args.run_root / ARM_DIRECTORIES[arm] / "infrastructure_failures.json"
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, Any]] = []
    try:
        for instance in instances:
            ledger.mark_arm(arm, status="running", current_task=instance["instance_id"])
            quota_wait_started: float | None = None
            while True:
                last_error: Exception | None = None
                for attempt in range(1, args.infrastructure_retries + 1):
                    try:
                        result = run_instance(instance, args, arm, ledger)
                        if result["status"] != "infrastructure_failure":
                            last_error = None
                            break
                        last_error = RuntimeError(
                            result.get("failure_reason") or result["status"]
                        )
                    except Exception as exc:  # keep the 100-task queue resumable
                        last_error = exc
                        error_root = (
                            args.run_root
                            / ARM_DIRECTORIES[arm]
                            / "i"
                            / instance["instance_id"]
                            / "errors"
                        )
                        error_root.mkdir(parents=True, exist_ok=True)
                        (error_root / f"attempt_{attempt}.log").write_text(
                            traceback.format_exc(), encoding="utf-8"
                        )
                    if last_error is not None and QUOTA_ERROR in str(last_error):
                        break
                    if attempt < args.infrastructure_retries:
                        time.sleep(min(60, 5 * attempt))
                if last_error is None:
                    break
                if QUOTA_ERROR not in str(last_error):
                    break
                if quota_wait_started is None:
                    quota_wait_started = time.monotonic()
                waited_hours = (time.monotonic() - quota_wait_started) / 3600
                if waited_hours >= args.quota_max_wait_hours:
                    last_error = RuntimeError(
                        f"{QUOTA_ERROR}: wait exceeded {args.quota_max_wait_hours:g}h"
                    )
                    break
                retry_at = datetime.now().astimezone() + timedelta(
                    seconds=args.quota_retry_seconds
                )
                ledger.mark_arm(
                    arm,
                    status="waiting_quota",
                    current_task=instance["instance_id"],
                    status_detail=QUOTA_ERROR,
                    retry_at=retry_at.isoformat(timespec="seconds"),
                )
                time.sleep(args.quota_retry_seconds)
                ledger.mark_arm(
                    arm, status="running", current_task=instance["instance_id"]
                )
            if last_error is not None:
                failure_record = {
                    "status": "infrastructure_failure",
                    "resolved": False,
                    "failure_reason": repr(last_error),
                    "usage": Usage().model_dump(),
                    "metrics": None,
                }
                ledger.record_task(arm=arm, task=instance, arm_result=failure_record)
                failures.append({"instance_id": instance["instance_id"], "error": repr(last_error)})
                failures_path.write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
    finally:
        ledger.mark_arm(arm, status="finished", current_task=None)
    failures_path.write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise SystemExit(f"{len(failures)} infrastructure failures; rerun this arm to resume")


if __name__ == "__main__":
    main()
