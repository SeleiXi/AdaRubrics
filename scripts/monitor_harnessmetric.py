"""Five-minute live dashboard for the four-arm SWE-bench run."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from adarubric.harnessmetric.ledger import ARM_NAMES


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _bar(done: int, total: int, width: int = 30) -> str:
    ratio = done / total if total else 0.0
    filled = min(width, round(width * ratio))
    return "[" + "#" * filled + "." * (width - filled) + f"] {done}/{total} {ratio:.1%}"


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _frame(path: Path) -> list[str]:
    data = _read(path)
    arms = data.get("arms", {})
    labels = {
        "plain_deepseek_v4_flash": "Plain + DS-v4-flash",
        "plain_hy3": "Plain + hy3",
        "harnessmetric_deepseek_v4_flash": "HarnessMetric + DS-v4-flash",
        "harnessmetric_hy3": "HarnessMetric + hy3",
    }
    lines = [
        "HarnessMetric x SWE-bench Verified (100 tasks)",
        datetime.now().astimezone().strftime("Updated: %Y-%m-%d %H:%M:%S %Z"),
        f"Ledger: {path}",
        "",
    ]
    for name in ARM_NAMES:
        state = arms.get(name, {})
        total = int(state.get("total", data.get("task_count", 100)))
        done = int(state.get("completed", 0))
        lines.append(f"{labels[name]}  [{state.get('status', 'pending')}]")
        lines.append(
            f"  {_bar(done, total)}  success={state.get('resolved', 0)}/{done} "
            f"censored={state.get('censored', 0)} infra={state.get('failed_infrastructure', 0)}"
        )
        lines.append(
            "  wall={:.1f}h  input={}  cache-read={}  output={}  turns={}".format(
                float(state.get("wall_seconds", 0)) / 3600,
                _format_tokens(int(state.get("input_tokens", 0))),
                _format_tokens(int(state.get("cache_read_input_tokens", 0))),
                _format_tokens(int(state.get("output_tokens", 0))),
                state.get("turns", 0),
            )
        )
        lines.append(f"  current={state.get('current_task') or 'none'}")
        if state.get("status_detail"):
            retry = f"; retry={state['retry_at']}" if state.get("retry_at") else ""
            lines.append(f"  detail={state['status_detail']}{retry}")
        lines.append("")
    comparisons = data.get("comparisons", {})
    mismatches = 0
    time_outliers = 0
    for task in comparisons.values():
        for item in task.values():
            mismatches += int(bool(item.get("success_mismatch_explanation")))
            time_outliers += int(bool(item.get("large_time_difference")))
    lines.extend(
        [
            f"Recorded paired success mismatches: {mismatches}",
            f"Recorded large wall-time differences: {time_outliers}",
            "The screen refreshes every 5 minutes. Ctrl+C stops only this monitor.",
        ]
    )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=300.0)
    args = parser.parse_args()
    first = True
    try:
        while True:
            lines = _frame(args.ledger.resolve())
            if not first:
                sys.stdout.write(f"\x1b[{len(lines)}F")
            for line in lines:
                sys.stdout.write("\x1b[2K" + line + "\n")
            sys.stdout.flush()
            first = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped; benchmark processes continue running.")


if __name__ == "__main__":
    main()
