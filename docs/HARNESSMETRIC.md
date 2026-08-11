# HarnessMetric

HarnessMetric converts AdaRubric's task-adaptive evaluation dimensions into an online
control interface for a frozen coding agent. The treatment loop is:

`generate metrics -> execute -> isolate and measure -> select unmet bottleneck ->`
`resume the same agent -> re-measure -> stop or refine`

The original issue is authoritative. Generated metrics are classified as hard task
requirements, repository-backed regression constraints, or exploratory probes.
Exploratory probes cannot block stopping or broaden the implementation. Verification
runs in a disposable repository clone so verifier edits cannot enter the submitted
patch.

## Why metrics are not injected initially by default

The preliminary static-rubric runs showed no static-only wins and several plain-only
wins. They also reproduced scope expansion (for example, promoting plausible boundary
cases into hard requirements). Therefore `--initial-metric-policy off` is the
pre-registered default: metrics are generated before execution but disclosed only as
measured, task-anchored bottleneck feedback after the first autonomous attempt. `hard`
and `all` remain available as ablations.

## Turn and timeout policy

The executor and verifier do not receive CodeBuddy's `--max-turns` option. Each
invocation has a 7,200-second wall-time safety watchdog, the online loop permits 12
refinements and 12 hours per task, and any watchdog/refinement truncation is marked
`censored` rather than counted as a benchmark failure. State, sessions, metrics, and
measurements are checkpointed after every phase.

## SWE-bench Verified four-arm run

After `uv sync --extra dev`, launch the fixed 100-task manifest with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\launch_swebench_100.ps1
```

This starts exactly four independent batch processes:

1. plain CodeBuddy + `deepseek-v4-flash`;
2. plain CodeBuddy + `hy3`;
3. HarnessMetric + CodeBuddy + `deepseek-v4-flash`;
4. HarnessMetric + CodeBuddy + `hy3`.

It also opens a separate terminal that refreshes every five minutes. The shared
`experiment.json` ledger records full task statements, generated metrics, official
success, end-to-end wall time, phase-level token usage, mismatched-success failure
analysis, and large runtime-difference explanations. Each arm is resumable by rerunning
the launcher.

### Resume rules (do not kill runners)

- Rerun `scripts\launch_swebench_100.ps1` to resume. It never kills processes; it only
  starts arms whose pid file is missing or points at a dead/stale process.
- Resume a subset with
  `powershell -ExecutionPolicy Bypass -File scripts\launch_swebench_100.ps1 -Only harnessmetric_deepseek_v4_flash,harnessmetric_hy3`.
- Do **not** `taskkill /T /F` the arm runner PID. That aborts mid-loop checkpoints and
  leaves `experiment.json` stuck at `status=running` even though nothing is alive.
  Agent wall-time limits already isolate-kill only the CodeBuddy child process.
- The monitor flags `running/DEAD` when the ledger claims an arm is active but its
  process is gone; use the launcher to resume instead of killing anything.
