"""Run public development commands inside a SWE-bench instance image."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a command is required")
    workspace = Path.cwd().resolve()
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--volume",
        f"{workspace}:/testbed",
        "--workdir",
        "/testbed",
        "--entrypoint",
        "bash",
        args.image,
        "-lc",
        'source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && exec "$@"',
        "harnessmetric-command",
        *args.command,
    ]
    raise SystemExit(subprocess.run(command, check=False).returncode)  # noqa: S603


if __name__ == "__main__":
    main()
