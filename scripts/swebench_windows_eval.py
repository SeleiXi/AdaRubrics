"""Invoke the official SWE-bench evaluator with LF artifacts on Windows."""

from __future__ import annotations

import argparse
from pathlib import Path

_write_text = Path.write_text


def _write_text_with_linux_newlines(
    self: Path,
    data: str,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
) -> int:
    return _write_text(
        self,
        data,
        encoding=encoding,
        errors=errors,
        newline="\n" if newline is None else newline,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()
    Path.write_text = _write_text_with_linux_newlines
    from swebench.harness.run_evaluation import main as evaluate

    report = evaluate(
        dataset_name="princeton-nlp/SWE-bench_Verified",
        split="test",
        instance_ids=[args.instance],
        predictions_path=args.prediction,
        max_workers=1,
        force_rebuild=False,
        cache_level="instance",
        clean=False,
        open_file_limit=8192,
        run_id=args.run_id,
        timeout=args.timeout,
        namespace="swebench",
        rewrite_reports=False,
        modal=False,
    )
    print(report)


if __name__ == "__main__":
    main()
