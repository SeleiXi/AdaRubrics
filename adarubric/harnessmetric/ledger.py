"""Concurrency-safe JSON ledger shared by four benchmark processes."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

ARM_NAMES = (
    "plain_deepseek_v4_flash",
    "plain_hy3",
    "harnessmetric_deepseek_v4_flash",
    "harnessmetric_hy3",
)


def _empty_arm(total: int) -> dict[str, Any]:
    return {
        "status": "pending",
        "total": total,
        "completed": 0,
        "resolved": 0,
        "censored": 0,
        "failed_infrastructure": 0,
        "wall_seconds": 0.0,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "turns": 0,
        "current_task": None,
        "pid": None,
        "status_detail": None,
        "retry_at": None,
    }


class RunLedger:
    def __init__(self, path: Path, *, lock_timeout: float = 120.0) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout = lock_timeout

    @contextmanager
    def _lock(self) -> Iterator[None]:
        started = time.monotonic()
        while True:
            try:
                self.lock_path.mkdir(parents=False)
                break
            except FileExistsError:
                if time.monotonic() - started > self.lock_timeout:
                    try:
                        age = time.time() - self.lock_path.stat().st_mtime
                    except OSError:
                        age = 0.0
                    if age > self.lock_timeout * 2:
                        self.lock_path.rmdir()
                        continue
                    raise TimeoutError(f"ledger lock held too long: {self.lock_path}") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            with suppress(OSError):
                self.lock_path.rmdir()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"ledger root must be an object: {self.path}")
        return cast(dict[str, Any], payload)

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # Windows: another worker may transiently hold the destination file lock
        # (e.g. os.replace target in use by a parallel process). Retry briefly.
        for attempt in range(30):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(0.5)

    def initialize(
        self,
        *,
        benchmark: str,
        manifest: str,
        task_count: int,
        initial_metric_policy: str,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if self.path.is_file():
                return
            self._write(
                {
                    "schema_version": 1,
                    "benchmark": benchmark,
                    "manifest": manifest,
                    "task_count": task_count,
                    "initial_metric_policy": initial_metric_policy,
                    "arms": {name: _empty_arm(task_count) for name in ARM_NAMES},
                    "tasks": {},
                    "comparisons": {},
                }
            )

    def mark_arm(
        self,
        arm: str,
        *,
        status: str,
        current_task: str | None = None,
        status_detail: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        with self._lock():
            data = self._read()
            state = data["arms"][arm]
            state["status"] = status
            state["current_task"] = current_task
            state["pid"] = os.getpid()
            state["status_detail"] = status_detail
            state["retry_at"] = retry_at
            self._write(data)

    def record_task(
        self,
        *,
        arm: str,
        task: dict[str, Any],
        arm_result: dict[str, Any],
    ) -> None:
        with self._lock():
            data = self._read()
            instance_id = task["instance_id"]
            record = data["tasks"].setdefault(
                instance_id,
                {
                    "instance_id": instance_id,
                    "repo": task.get("repo"),
                    "base_commit": task.get("base_commit"),
                    "difficulty": task.get("difficulty"),
                    "problem_statement": task.get("problem_statement"),
                    "arms": {},
                },
            )
            record["arms"][arm] = arm_result
            self._refresh_arm(data, arm)
            self._refresh_comparison(data, instance_id)
            self._write(data)

    @staticmethod
    def _refresh_arm(data: dict[str, Any], arm: str) -> None:
        records = [
            task["arms"][arm] for task in data["tasks"].values() if arm in task.get("arms", {})
        ]
        state = data["arms"][arm]
        completed = [record for record in records if record.get("status") == "completed"]
        state.update(
            {
                "completed": len(completed),
                "resolved": sum(bool(record.get("resolved")) for record in completed),
                "censored": sum(record.get("status") == "censored" for record in records),
                "failed_infrastructure": sum(
                    record.get("status") == "infrastructure_failure" for record in records
                ),
            }
        )
        for key in (
            "wall_seconds",
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
            "turns",
        ):
            state[key] = sum(float(record.get("usage", {}).get(key, 0) or 0) for record in records)
            if key != "wall_seconds":
                state[key] = int(state[key])

    @staticmethod
    def _failure_reason(record: dict[str, Any]) -> str:
        if record.get("failure_reason"):
            return str(record["failure_reason"])
        score = record.get("official_score", {})
        report = score.get("official_report", {}) if isinstance(score, dict) else {}
        tests = report.get("tests_status", {}) if isinstance(report, dict) else {}
        ftp = tests.get("FAIL_TO_PASS", {}).get("failure", [])
        ptp = tests.get("PASS_TO_PASS", {}).get("failure", [])
        if ftp and ptp:
            return f"missed {len(ftp)} target tests and regressed {len(ptp)} tests"
        if ftp:
            return f"failed {len(ftp)} hidden target tests"
        if ptp:
            return f"regressed {len(ptp)} previously passing tests"
        if record.get("empty_patch"):
            return "executor produced an empty patch"
        return "official grader marked the patch unresolved without a more specific report"

    @classmethod
    def _refresh_comparison(cls, data: dict[str, Any], instance_id: str) -> None:
        arms = data["tasks"][instance_id].get("arms", {})
        comparisons: dict[str, Any] = {}
        for model, suffix in (
            ("deepseek-v4-flash", "deepseek_v4_flash"),
            ("hy3", "hy3"),
        ):
            plain = arms.get(f"plain_{suffix}")
            harness = arms.get(f"harnessmetric_{suffix}")
            if not plain or not harness:
                continue
            item: dict[str, Any] = {
                "model": model,
                "plain_resolved": bool(plain.get("resolved")),
                "harnessmetric_resolved": bool(harness.get("resolved")),
                "success_mismatch_explanation": None,
                "large_time_difference": None,
            }
            if item["plain_resolved"] != item["harnessmetric_resolved"]:
                loser_name = "harnessmetric" if item["plain_resolved"] else "plain"
                loser = harness if loser_name == "harnessmetric" else plain
                item["success_mismatch_explanation"] = {
                    "winner": "plain" if item["plain_resolved"] else "harnessmetric",
                    "loser": loser_name,
                    "reason": cls._failure_reason(loser),
                }
            plain_wall = float(plain.get("usage", {}).get("wall_seconds", 0) or 0)
            harness_wall = float(harness.get("usage", {}).get("wall_seconds", 0) or 0)
            smaller = min(plain_wall, harness_wall)
            ratio = max(plain_wall, harness_wall) / smaller if smaller > 0 else 0.0
            delta = abs(plain_wall - harness_wall)
            if ratio >= 1.5 or delta >= 600:
                slower = "plain" if plain_wall > harness_wall else "harnessmetric"
                reason = (
                    "HarnessMetric generation, isolated verification, and refinement calls "
                    "are the directly observed extra phases. Exact runtime attribution remains "
                    "post-hoc."
                    if slower == "harnessmetric"
                    else "The metric feedback may have reduced exploration, but this is a "
                    "post-hoc inference rather than a causal attribution."
                )
                item["large_time_difference"] = {
                    "plain_seconds": plain_wall,
                    "harnessmetric_seconds": harness_wall,
                    "ratio": ratio,
                    "slower": slower,
                    "explanation": reason,
                }
            comparisons[model] = item
        if comparisons:
            data["comparisons"][instance_id] = comparisons
