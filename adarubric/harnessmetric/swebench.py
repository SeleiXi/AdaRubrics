"""SWE-bench Verified preparation, patch capture, and official grading."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any


def run(command: list[str], *, cwd: Path, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
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


def load_rows(rows_dir: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(rows_dir.glob("rows-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in payload["rows"]:
            row = item["row"]
            rows[row["instance_id"]] = row
    return rows


def image_name(instance_id: str) -> str:
    escaped = instance_id.lower().replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{escaped}:latest"


def docker(
    *arguments: str, cwd: Path, timeout: int = 3600, required: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = run(["docker", *arguments], cwd=cwd, timeout=timeout)
    if required and completed.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(arguments)} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return completed


def prepare_pristine(instance: dict[str, Any], root: Path, image: str) -> Path:
    pristine = root / "pristine"
    if (pristine / ".git").exists():
        return pristine
    if pristine.exists():
        shutil.rmtree(pristine)
    pristine.mkdir(parents=True)
    container = (
        "harnessmetric-copy-"
        + hashlib.sha256(f"{instance['instance_id']}:{root}".encode()).hexdigest()[:12]
    )
    docker("create", "--name", container, image, "tail", "-f", "/dev/null", cwd=root)
    docker("start", container, cwd=root)
    try:
        docker_executable = shutil.which("docker.exe") or shutil.which("docker")
        if docker_executable is None:
            raise RuntimeError("docker was not found on PATH")
        process = subprocess.Popen(  # noqa: S603
            [
                docker_executable,
                "exec",
                container,
                "tar",
                "-chf",
                "-",
                "-C",
                "/testbed",
                ".",
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                parts = Path(member.name.replace("/", "\\")).parts
                if Path(member.name).is_absolute() or ".." in parts:
                    raise RuntimeError(f"unsafe path in image archive: {member.name}")
                archive.extract(member, path=pristine)
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        if process.wait() != 0:
            raise RuntimeError(f"could not stream official image: {stderr}")
    finally:
        docker("rm", "-f", container, cwd=root, required=False)
    for key, value in (("core.symlinks", "false"), ("core.filemode", "false")):
        configured = run(["git", "config", key, value], cwd=pristine)
        if configured.returncode != 0:
            raise RuntimeError(configured.stderr)
    checked = run(["git", "reset", "--hard", instance["base_commit"]], cwd=pristine)
    if checked.returncode != 0:
        raise RuntimeError(f"image lacks benchmark base commit: {checked.stderr}")
    run(["git", "clean", "-fdx"], cwd=pristine)
    return pristine


def prepare_workspace(
    *,
    pristine: Path,
    root: Path,
    base_commit: str,
    image: str,
    helper_script: Path,
) -> Path:
    workspace = root / "workspace"
    if (workspace / ".git").exists():
        return workspace
    root.mkdir(parents=True, exist_ok=True)
    cloned = run(
        ["git", "clone", "--shared", str(pristine.resolve()), str(workspace.resolve())],
        cwd=root,
    )
    if cloned.returncode != 0:
        raise RuntimeError(cloned.stderr)
    checked = run(["git", "checkout", "--detach", base_commit], cwd=workspace)
    if checked.returncode != 0:
        raise RuntimeError(checked.stderr)
    helper = workspace / ".harnessmetric"
    helper.mkdir()
    shutil.copy2(helper_script, helper / "run_tests.py")
    (helper / "image.txt").write_text(image + "\n", encoding="utf-8")
    return workspace


def repository_context(workspace: Path, instruction: str, limit: int = 40000) -> str:
    listing = run(["git", "ls-files"], cwd=workspace).stdout.splitlines()
    chunks = ["Tracked files (first 500):\n" + "\n".join(listing[:500])]
    for name in ("README.md", "README.rst", "README.txt", "pyproject.toml", "setup.py"):
        path = workspace / name
        if path.is_file():
            chunks.append(
                f"--- {name} ---\n" + path.read_text(encoding="utf-8", errors="replace")[:12000]
            )
    words = {
        word.strip("`'\".,:;()[]{}").casefold()
        for word in instruction.replace("/", " ").split()
        if len(word.strip("`'\".,:;()[]{}")) >= 5
    }
    relevant = [
        name
        for name in listing
        if Path(name).suffix in {".py", ".js", ".ts", ".java"}
        and any(word in Path(name).stem.casefold() for word in words)
    ][:5]
    for name in relevant:
        path = workspace / name
        chunks.append(
            f"--- task-relevant source: {name} ---\n"
            + path.read_text(encoding="utf-8", errors="replace")[:12000]
        )
    return "\n\n".join(chunks)[:limit]


def model_patch(workspace: Path, base_commit: str) -> str:
    untracked = run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace
    ).stdout.splitlines()
    untracked = [path for path in untracked if not path.startswith(".harnessmetric/")]
    if untracked:
        added = run(["git", "add", "-N", "--", *untracked], cwd=workspace)
        if added.returncode != 0:
            raise RuntimeError(added.stderr)
    diff = run(
        [
            "git",
            "-c",
            "core.fileMode=false",
            "diff",
            "--binary",
            base_commit,
            "--",
            ".",
            ":(exclude).harnessmetric",
        ],
        cwd=workspace,
    )
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr)
    return diff.stdout


def grade(
    *,
    instance: dict[str, Any],
    arm: str,
    model: str,
    patch: str,
    root: Path,
    harness_python: Path,
    eval_script: Path,
    timeout: int,
) -> dict[str, Any]:
    if not patch.strip():
        return {"completed": True, "resolved": False, "empty_patch": True}
    grade_root = root / "official_grade"
    grade_root.mkdir(parents=True, exist_ok=True)
    model_name = f"{model}-{arm}".replace("/", "__")
    prediction = {
        "instance_id": instance["instance_id"],
        "model_name_or_path": model_name,
        "model_patch": patch,
    }
    prediction_path = grade_root / "prediction.jsonl"
    prediction_path.write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    run_id = "hm-" + hashlib.sha256(f"{instance['instance_id']}:{arm}:v1".encode()).hexdigest()[:12]
    completed = run(
        [
            str(harness_python.resolve()),
            str(eval_script.resolve()),
            "--prediction",
            str(prediction_path.resolve()),
            "--instance",
            instance["instance_id"],
            "--run-id",
            run_id,
            "--timeout",
            str(timeout),
        ],
        cwd=grade_root,
        timeout=timeout + 900,
    )
    (grade_root / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (grade_root / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    report_path = (
        grade_root
        / "logs"
        / "run_evaluation"
        / run_id
        / model_name
        / instance["instance_id"]
        / "report.json"
    )
    if not report_path.is_file():
        return {
            "completed": False,
            "resolved": False,
            "return_code": completed.returncode,
            "error": "official report.json was not produced",
        }
    report = json.loads(report_path.read_text(encoding="utf-8"))[instance["instance_id"]]
    return {
        "completed": True,
        "resolved": bool(report["resolved"]),
        "empty_patch": False,
        "return_code": completed.returncode,
        "official_report": report,
    }
