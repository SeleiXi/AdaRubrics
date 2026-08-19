"""opencode agent adapter for terminal-bench.

Drives the local opencode CLI (free DeepSeek models like
opencode/deepseek-v4-flash-free) as a terminal-bench agent. Same loop as
CodeBuddyAgent: parse command JSON -> execute in tmux -> feed output back.

Usage:
  set PYTHONPATH to this directory, then:
  tb run --agent-import-path opencode_agent:OpencodeAgent \
         --agent-kwarg model_name=opencode/deepseek-v4-flash-free \
         --dataset-name terminal-bench-core --dataset-version 0.1.1 ...
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.agents.terminus_2.terminus_2 import Terminus2
from terminal_bench.terminal.tmux_session import TmuxSession


class OpencodeAgent(Terminus2):
    """Terminus2-style agent whose LLM is the local opencode CLI."""

    @staticmethod
    def name() -> str:
        return "opencode"

    def __init__(self, model_name: str = "opencode/deepseek-v4-flash-free", **kwargs):
        # Bypass Terminus2.__init__ (it builds a LiteLLM); call BaseAgent init.
        BaseAgent.__init__(self, **kwargs)
        self._model_name = model_name
        self._parser_name = kwargs.get("parser_name", "json")
        self._parser = self._get_parser()
        self._prompt_template = self._get_prompt_template_path().read_text()
        self._timeout_template = self._get_timeout_template_path().read_text()
        self._logger = __import__("terminal_bench.utils.logger", fromlist=["logger"]).logger.getChild(__name__)
        self._max_episodes = kwargs.get("max_episodes") or 1000000
        self._timestamped_markers = []
        self._pending_completion = False
        self._chat = SimpleNamespace(_messages=[], total_input_tokens=0, total_output_tokens=0)

    def _query_llm(
        self,
        chat,
        prompt: str,
        logging_paths,
        original_instruction: str = "",
        session=None,
    ) -> str:
        """Call opencode CLI with a JSON schema matching terminus output."""
        logging_path, prompt_path, response_path = logging_paths
        env_note = (
            "IMPORTANT ENVIRONMENT NOTE: You are driving a terminal inside an "
            "isolated remote Linux container via tmux. The terminal output in "
            "the prompt below is the container's actual state. Any filesystem, "
            "OS, or tool information visible in your own session context (project "
            "files, memory, cwd) refers to a DIFFERENT Windows host machine and "
            "must be IGNORED for this task. Do not diagnose or report on the host "
            "environment; act only on the container terminal state shown.\n\n"
        )
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

        prompt = env_note + prompt
        # opencode has no JSON-schema mode; instruct the model to emit exactly
        # one JSON object (no markdown fences, no commentary) so the terminus
        # parser can consume it.
        prompt += (
            "\n\nYour entire reply must be one raw JSON object with NO markdown "
            "fence, NO code block markers, and NO text outside the JSON. It must "
            f"match this schema exactly:\n{json.dumps(schema, ensure_ascii=False)}"
        )
        if prompt_path is not None:
            prompt_path.write_text(prompt)

        executable = shutil.which("opencode")
        if executable is None:
            raise RuntimeError("opencode CLI not found on PATH")
        command = [
            executable,
            "run",
            "--model",
            self._model_name,
            "--pure",
            "--title",
            "tb-agent",
        ]

        env = dict(os.environ)
        # opencode reaches its model API through the local clash proxy; set it
        # explicitly so the tb process itself need not carry proxy env vars
        # (which leak into the task containers and break container apt).
        env["HTTPS_PROXY"] = env.get("HTTPS_PROXY") or "http://127.0.0.1:7890"
        env["HTTP_PROXY"] = env.get("HTTP_PROXY") or "http://127.0.0.1:7890"
        env["NO_PROXY"] = env.get("NO_PROXY") or "localhost,127.0.0.1"
        # Force UTF-8 for the child so subprocess's internal reader threads
        # decode its output as UTF-8 instead of the Windows cp1252 codepage.
        env["PYTHONIOENCODING"] = "utf-8"
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
            self._logger.warning("opencode timed out after 600s")
            raise RuntimeError("opencode timed out") from exc

        if logging_path is not None:
            logging_path.write_text(completed.stderr or "", encoding="utf-8")
        if response_path is not None:
            response_path.write_text(completed.stdout or "", encoding="utf-8")

        if completed.returncode != 0:
            self._logger.warning("opencode exited %s: %s", completed.returncode, completed.stderr[:2000])
            raise RuntimeError(f"opencode exited {completed.returncode}")

        output = completed.stdout or ""
        # opencode prints banner / build info before the model response; keep
        # only the final JSON object block.
        try:
            start = output.index("{")
            end = output.rindex("}")
            return output[start : end + 1]
        except ValueError:
            return output

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
