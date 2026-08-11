#!/usr/bin/env python
"""Run a batch of terminal-bench tasks with the CodeBuddy agent (hy3) and
record per-task metrics to a JSON ledger, mirroring the hm100 experiment.

Usage:
  tb_runner.py --task-glob <glob> --run-root <dir> [--ledger <json>] \
               [--n-concurrent <n>] [--max-tasks <n>]

The tb CLI is invoked once per batch (it runs tasks sequentially inside a run);
we use --n-concurrent for parallelism and --task-id for selection.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # AutoMetric/
TB_VENV = ROOT / "tmp/runtime/tb-venv/Scripts"
TB = TB_VENV / "tb.exe"
ADAPTOR_DIR = ROOT / "AdaRubrics/scripts"
DATASET_PATH = ROOT / "tmp/tb-repo/tasks"


def run_tb(task_glob: str, run_root: Path, n_concurrent: int) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    env["HTTP_PROXY"] = "http://127.0.0.1:7890"
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    env["PYTHONPATH"] = str(ADAPTOR_DIR)
    cmd = [
        str(TB), "run",
        "--agent-import-path", "codebuddy_agent:CodeBuddyAgent",
        "--agent-kwarg", "model_name=hy3",
        "--dataset-path", str(DATASET_PATH),
        "--task-id", task_glob,
        "--output-path", str(run_root),
        "--n-concurrent", str(n_concurrent),
        "--no-upload-results",
    ]
    print(f"[tb] running: {' '.join(cmd)}", flush=True)
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=4 * 3600)
    if completed.returncode != 0:
        print(f"[tb] non-zero exit {completed.returncode}", flush=True)
    # Find newest run dir
    runs = sorted(run_root.glob("2026-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if runs:
        return runs[0]
    return run_root


def summarize_run(run_dir: Path) -> list[dict]:
    results_path = run_dir / "results.json"
    if not results_path.exists():
        return []
    data = json.loads(results_path.read_text(encoding="utf-8"))
    out = []
    for r in data.get("results", []):
        out.append({
            "task_id": r.get("task_id"),
            "is_resolved": r.get("is_resolved"),
            "failure_mode": r.get("failure_mode"),
            "total_input_tokens": r.get("total_input_tokens"),
            "total_output_tokens": r.get("total_output_tokens"),
            "trial_name": r.get("trial_name"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-glob", required=True)
    ap.add_argument("--run-root", type=Path, default=ROOT / "runs/tb")
    ap.add_argument("--ledger", type=Path, default=ROOT / "runs/tb/ledger.json")
    ap.add_argument("--n-concurrent", type=int, default=3)
    ap.add_argument("--label", default="tb-batch")
    args = ap.parse_args()

    ledger_path = args.ledger
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    else:
        ledger = {"runs": [], "tasks": {}}

    run_dir = run_tb(args.task_glob, args.run_root, args.n_concurrent)
    results = summarize_run(run_dir)

    entry = {
        "label": args.label,
        "task_glob": args.task_glob,
        "run_dir": str(run_dir),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": results,
    }
    ledger["runs"].append(entry)
    for r in results:
        tid = r["task_id"]
        if tid:
            ledger["tasks"].setdefault(tid, []).append(r)

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tb] done. {len(results)} tasks recorded to {ledger_path}", flush=True)


if __name__ == "__main__":
    main()
