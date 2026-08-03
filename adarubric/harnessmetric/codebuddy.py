"""Auditable, resumable CodeBuddy CLI adapter used by HarnessMetric."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adarubric.harnessmetric.models import Usage


@dataclass(frozen=True)
class CodeBuddyResult:
    return_code: int
    usage: Usage
    session_id: str | None
    model: str | None
    final_message: str
    termination_reason: str | None = None


def _launcher() -> list[str]:
    executable = shutil.which("codebuddy.cmd") or shutil.which("codebuddy.exe")
    if executable is None:
        raise RuntimeError("CodeBuddy CLI was not found on PATH")
    if Path(executable).suffix.casefold() != ".cmd":
        return [executable]
    node = shutil.which("node.exe") or shutil.which("node")
    entry = (
        Path(executable).parent
        / "node_modules"
        / "@tencent-ai"
        / "codebuddy-code"
        / "bin"
        / "codebuddy"
    )
    if node is None or not entry.is_file():
        raise RuntimeError("Could not resolve the CodeBuddy Node entry point")
    return [node, str(entry)]


def _events(stdout: str) -> list[dict[str, Any]]:
    payload = json.loads(stdout)
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, list):
        raise ValueError("CodeBuddy JSON output is neither an object nor an array")
    return [item for item in payload if isinstance(item, dict)]


def extract_json_object(message: str) -> object:
    """Parse direct JSON or the first JSON object embedded in rendered output."""

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        payload = None
        for index, character in enumerate(message):
            if character != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(message[index:])
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise ValueError("no JSON object found in CodeBuddy output") from None
    if isinstance(payload, dict) and payload.get("type") == "json":
        return payload.get("parameters")
    return payload


def run_codebuddy(
    *,
    workspace: Path,
    prompt: str,
    event_log: Path,
    stderr_log: Path,
    model: str,
    effort: str = "medium",
    timeout_seconds: int = 7200,
    tools: str = "default",
    session_id: str | None = None,
    resume_session_id: str | None = None,
    persist_session: bool = True,
    max_turns: int | None = None,
) -> CodeBuddyResult:
    """Run one agent invocation.

    ``max_turns`` is deliberately optional and omitted by the benchmark runner. A
    wall-time interruption is recorded as censored instead of a task failure.
    """

    command = [
        *_launcher(),
        "--print",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--model",
        model,
        "--effort",
        effort,
        "--tools",
        tools,
        "--setting-sources",
        "project",
    ]
    if tools:
        command.extend(["--permission-mode", "bypassPermissions"])
    if resume_session_id:
        command.extend(["--resume", resume_session_id])
    elif session_id:
        command.extend(["--session-id", session_id])
    if not persist_session:
        command.append("--no-session-persistence")
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])

    event_log.parent.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    started = time.perf_counter()
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    termination_reason: str | None = None
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        termination_reason = "agent_timeout"
        if os.name == "nt":
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
            if taskkill is None:
                raise RuntimeError("taskkill was not found while stopping CodeBuddy") from None
            subprocess.run(  # noqa: S603
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
        stderr += f"\nAgent wall-time safety limit ({timeout_seconds}s) exceeded\n"

    wall_seconds = time.perf_counter() - started
    event_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")

    result_event: dict[str, Any] = {}
    detected_model: str | None = None
    try:
        events = _events(stdout)
        result_event = next(
            (event for event in reversed(events) if event.get("type") == "result"), {}
        )
        for event in events:
            provider = event.get("providerData") or {}
            if provider.get("model"):
                detected_model = str(provider["model"])
    except (json.JSONDecodeError, ValueError):
        pass

    usage_payload = result_event.get("usage") or {}
    final = result_event.get("result", "")
    if not isinstance(final, str):
        final = json.dumps(final, ensure_ascii=False)
    if termination_reason is None and "max turns" in stderr.casefold():
        termination_reason = "max_turns"
    return CodeBuddyResult(
        return_code=process.returncode if process.returncode is not None else 124,
        usage=Usage(
            wall_seconds=wall_seconds,
            input_tokens=int(usage_payload.get("input_tokens", 0)),
            cache_creation_input_tokens=int(usage_payload.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage_payload.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage_payload.get("output_tokens", 0)),
            turns=int(result_event.get("num_turns", 0)),
        ),
        session_id=result_event.get("session_id") or resume_session_id or session_id,
        model=detected_model,
        final_message=final,
        termination_reason=termination_reason,
    )
