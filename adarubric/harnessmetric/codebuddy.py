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
    infrastructure_error: str | None = None


def _infrastructure_error(stderr: str) -> str | None:
    lowered = stderr.casefold()
    if "429" in lowered and (
        "额度" in stderr  # 额度已用尽 / 超出频率限制 / 频率超限 etc.
        or "频率" in stderr
        or "quota" in lowered
        or "rate" in lowered
        or "limit" in lowered
    ):
        return "codebuddy_quota_exhausted"
    return None


def _launcher() -> list[str]:
    # Windows ships codebuddy.cmd/exe; Linux/macOS expose a plain `codebuddy`.
    executable = (
        shutil.which("codebuddy.cmd")
        or shutil.which("codebuddy.exe")
        or shutil.which("codebuddy")
    )
    if executable is None:
        # Fallback to known npm-global install locations when the worker's PATH
        # is stale (e.g. codebuddy reinstalled while a long-lived worker runs).
        import os
        candidates = [
            Path(os.path.expanduser("~")) / "AppData" / "Roaming" / "npm" / "codebuddy.cmd",
            Path(os.path.expanduser("~")) / "AppData" / "Roaming" / "npm" / "codebuddy.exe",
            Path("/usr/local/bin/codebuddy"),
            Path("/usr/bin/codebuddy"),
        ]
        executable = next((str(c) for c in candidates if c.is_file()), None)
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

    is_opencode = model.startswith("opencode/")
    if is_opencode:
        # Route through the opencode CLI (free models like opencode/hy3-free).
        executable = shutil.which("opencode")
        if executable is None:
            raise RuntimeError("opencode CLI not found on PATH")
        command = [
            executable,
            "run",
            "--model",
            model,
            "--pure",
            "--title",
            "hm-agent",
        ]
    else:
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
    if not is_opencode:
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
    if is_opencode:
        # opencode prints a banner ("> build · model") then the raw reply text.
        reply = stdout
        banner = reply.find("> build")
        if banner != -1:
            reply = reply[banner:]
            nl = reply.find("\n")
            if nl != -1:
                reply = reply[nl + 1 :]
        result_event = {"result": reply.strip(), "usage": {}}
        detected_model = model
    else:
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
        infrastructure_error=_infrastructure_error(stderr),
    )


def run_opencode(
    *,
    workspace: Path,
    prompt: str,
    event_log: Path,
    stderr_log: Path,
    model: str = "opencode/deepseek-v4-flash-free",
    effort: str = "medium",
    timeout_seconds: int = 7200,
    tools: str = "default",
    session_id: str | None = None,
    resume_session_id: str | None = None,
    persist_session: bool = True,
    max_turns: int | None = None,
) -> CodeBuddyResult:
    """Run one agent invocation through the opencode CLI (free models).

    Mirrors run_codebuddy's interface so the benchmark runner can switch agents
    with a single flag. opencode emits one JSON object per line in --format json.
    """
    opencode = shutil.which("opencode")
    if opencode is None:
        raise RuntimeError("opencode CLI was not found on PATH")
    command = [
        opencode,
        "run",
        "-m",
        model,
        "--dir",
        str(workspace),
        "--format",
        "json",
    ]
    if resume_session_id:
        command.extend(["-s", resume_session_id])
    elif session_id:
        command.extend(["--title", f"hm-{session_id[-16:] if session_id else 'run'}"])
    # Pass the prompt via stdin: Windows limits command-line length (~32K),
    # and HM prompts (system prompt + schema + task + repo context) exceed it.
    # opencode run reads a message from stdin when no message argument is given.

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
                raise RuntimeError("taskkill was not found while stopping opencode") from None
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

    # Parse opencode JSONL events.
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    final = ""
    detected_session: str | None = None
    usage = Usage()
    for ev in events:
        if ev.get("sessionID"):
            detected_session = ev["sessionID"]
        if ev.get("type") == "text" and isinstance(ev.get("part"), dict):
            text = ev["part"].get("text")
            if isinstance(text, str) and text.strip():
                final = text
        if ev.get("type") == "step_finish" and isinstance(ev.get("part"), dict):
            toks = (ev["part"].get("tokens") or {})
            usage.input_tokens += int(toks.get("input", 0) or 0)
            usage.output_tokens += int(toks.get("output", 0) or 0)
            usage.cache_read_input_tokens += int(toks.get("cache", {}).get("read", 0) or 0)
            usage.cache_creation_input_tokens += int(toks.get("cache", {}).get("write", 0) or 0)
    usage.wall_seconds = wall_seconds

    return CodeBuddyResult(
        return_code=process.returncode if process.returncode is not None else 124,
        usage=usage,
        session_id=detected_session or resume_session_id or session_id,
        model=model,
        final_message=final,
        termination_reason=termination_reason,
        infrastructure_error=_infrastructure_error(stderr),
    )


def run_agent(runner: str, **kwargs: Any) -> CodeBuddyResult:
    """Dispatch a single agent invocation to codebuddy or opencode."""
    if runner == "opencode":
        return run_opencode(**kwargs)
    return run_codebuddy(**kwargs)
