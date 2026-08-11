"""CodeBuddy agent adapter for terminal-bench.

Runs the local CodeBuddy CLI (model hy3) as a terminal-bench agent.
Inherits Terminus2's loop (parse command JSON -> execute in tmux -> feed
output back) but replaces the LiteLLM call with the CodeBuddy CLI, which
is given a JSON-schema so its reply is the same structure terminus expects.

Usage:
  set PYTHONPATH to this directory, then:
  tb run --agent codebuddy_agent:CodeBuddyAgent --model hy3 \
         --dataset-name terminal-bench-core --dataset-version 0.1.1 ...
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.agents.terminus_2.terminus_2 import Terminus2
from terminal_bench.terminal.tmux_session import TmuxSession


def _find_codebuddy() -> str:
    exe = shutil.which("codebuddy.cmd") or shutil.which("codebuddy.exe")
    if exe:
        return exe
    node = shutil.which("node.exe") or shutil.which("node")
    entry = (
        Path("C:/Users/Karsa/AppData/Roaming/npm/node_modules/@tencent-ai")
        / "codebuddy-code"
        / "bin"
        / "codebuddy"
    )
    if node and entry.is_file():
        return f"{node} {entry}"
    raise RuntimeError("CodeBuddy CLI not found")


class CodeBuddyAgent(Terminus2):
    """Terminus2-style agent whose LLM is the local CodeBuddy CLI."""

    @staticmethod
    def name() -> str:
        return "codebuddy"

    def __init__(self, model_name: str = "hy3", **kwargs):
        # Bypass Terminus2.__init__ (it builds a LiteLLM); call BaseAgent init.
        BaseAgent.__init__(self, **kwargs)
        self._model_name = model_name
        self._parser_name = kwargs.get("parser_name", "json")
        self._parser = self._get_parser()
        self._prompt_template = self._get_prompt_template_path().read_text()
        self._timeout_template = self._get_timeout_template_path().read_text()
        self._logger = __import__("terminal_bench.utils.logger", fromlist=["logger"]).logger.getChild(__name__)
        self._max_episodes = kwargs.get("max_episodes") or 1000000
        self._chat = None
        self._timestamped_markers = []
        self._pending_completion = False
        self._executable = _find_codebuddy()
        # Dummy chat so inherited loop helpers (proactive summarization etc.)
        # don't crash; _query_llm here never touches it.
        self._chat = SimpleNamespace(_messages=[], total_input_tokens=0, total_output_tokens=0)

    def _query_llm(
        self,
        chat,
        prompt: str,
        logging_paths,
        original_instruction: str = "",
        session=None,
    ) -> str:
        """Call CodeBuddy CLI with a JSON schema matching terminus output."""
        logging_path, prompt_path, response_path = logging_paths
        # The task is executed inside a remote Linux container (terminal-bench
        # drives a tmux session). CodeBuddy's own session context (AutoMetric
        # project files, memory) reflects the Windows host and must not be
        # mistaken for the container environment.
        env_note = (
            "IMPORTANT ENVIRONMENT NOTE: You are driving a terminal inside an "
            "isolated remote Linux container via tmux. The terminal output in "
            "the prompt below is the container's actual state. Any filesystem, "
            "OS, or tool information visible in your own session context (project "
            "files, memory, cwd) refers to a DIFFERENT Windows host machine and "
            "must be IGNORED for this task. Do not diagnose or report on the host "
            "environment; act only on the container terminal state shown.\n\n"
        )
        prompt = env_note + prompt
        if prompt_path is not None:
            prompt_path.write_text(prompt)

        schema = {
            "type": "object",
            "properties": {
                "analysis": {"type": "string"},
                "plan": {"type": "string"},
                "commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "keystrokes": {"type": "string"},
                            "duration": {"type": "number"},
                        },
                        "required": ["keystrokes"],
                    },
                },
                "task_complete": {"type": "boolean"},
            },
            "required": ["analysis", "plan", "commands"],
        }

        command = [
            self._executable,
            "--print",
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--model",
            self._model_name,
            "--effort",
            "medium",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--setting-sources",
            "project",
            "--no-session-persistence",
            "--max-turns",
            "8",
            "--permission-mode",
            "bypassPermissions",
            # tb drives execution in the container itself; CodeBuddy must only
            # emit the JSON command plan, not run tools on the host. Disabling
            # tools stops it from executing diagnostics in the wrong environment.
            "--tools",
            "",
        ]
        # Keep the inherited working directory (AutoMetric project root):
        # running CodeBuddy from a bare temp dir made it return empty replies.
        # Clear proxy vars: CodeBuddy talks to its local API directly; the
        # clash proxy env vars inherited from the tb process can break it.
        env = dict(__import__("os").environ)
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(key, None)
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=600,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            self._logger.warning("CodeBuddy timed out after 600s")
            stdout = (exc.stdout or "").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return stdout

        stdout = completed.stdout or ""
        if response_path is not None:
            response_path.write_text(stdout)
        # Parse the final result event's text output (codebuddy --print json wraps in events).
        text = self._extract_text_from_events(stdout)
        if not text.strip():
            self._logger.warning(f"CodeBuddy empty reply; stderr: {(completed.stderr or '')[:500]}")
        return text

    @staticmethod
    def _extract_text_from_events(stdout: str) -> str:
        """CodeBuddy --print --output-format json returns a list of events.
        Return the schema JSON: prefer the last StructuredOutput call's
        arguments (codebuddy emits schema JSON via that tool), then any
        assistant text that parses as JSON with a "commands" key."""
        try:
            events = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if not isinstance(events, list):
            return stdout
        structured = None
        candidate = None
        last_assistant = None
        for ev in events:
            if ev.get("type") == "function_call":
                if ev.get("name") == "StructuredOutput":
                    args = ev.get("arguments")
                    if isinstance(args, str):
                        structured = args
                continue
            if ev.get("type") != "message" or ev.get("role") != "assistant":
                continue
            content = ev.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not (isinstance(item, dict) and isinstance(item.get("text"), str)):
                    continue
                text = item["text"]
                if not text.strip():
                    continue
                last_assistant = text
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and "commands" in parsed:
                    candidate = text
        if structured is not None:
            return structured
        return candidate if candidate is not None else (last_assistant or stdout)

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        time_limit_seconds: float | None = None,
    ) -> AgentResult:
        initial_prompt = self._prompt_template.format(
            instruction=instruction,
            terminal_state=self._limit_output_length(session.get_incremental_output()),
        )
        self._run_agent_loop(initial_prompt, session, self._chat, logging_dir, instruction)
        return AgentResult(
            total_input_tokens=0,
            total_output_tokens=0,
            failure_mode=FailureMode.NONE,
            timestamped_markers=self._timestamped_markers,
        )
