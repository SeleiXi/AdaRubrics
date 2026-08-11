# worker_supervisor.py
# Watches the HM workers. If a worker process dies while its task group still
# has infra_failure tasks in the ledger, relaunch it with the recorded args.
# Complements docker_worker_watchdog.py (which restores the daemon).
import json
import os
import subprocess
import time
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # AutoMetric/
RUN_ROOT = os.path.join(ROOT, "runs", "hm100")
LEDGER = os.path.join(RUN_ROOT, "experiment.json")
CONFIG = os.path.join(RUN_ROOT, "worker_config.json")
LOG = os.path.join(RUN_ROOT, "supervisor.log")
REPO = os.path.join(ROOT, "AdaRubrics")
HARNESS_PY = os.path.join(RUN_ROOT, "..", "runtime", "swebench-venv", "Scripts", "python.exe")
RUNNER = os.path.join(REPO, "scripts", "run_swebench_harnessmetric.py")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def alive(pid):
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True, timeout=15)
        return pid in r.stdout
    except Exception:
        return False


def base_args():
    return [
        RUNNER,
        "--manifest", os.path.join(ROOT, "runs", "swebench-100", "manifest.json"),
        "--rows-dir", os.path.join(ROOT, "tmp", "swebench"),
        "--run-root", RUN_ROOT,
        "--ledger", LEDGER,
        "--harness-python", HARNESS_PY,
        "--effort", "medium",
        "--initial-metric-policy", "off",
        "--agent-timeout", "7200",
        "--generator-timeout", "7200",
        "--verifier-timeout", "7200",
        "--grade-timeout", "7200",
        "--max-refinements", "12",
        "--max-loop-hours", "48",
        "--infrastructure-retries", "5",
        "--quota-retry-seconds", "300",
        "--quota-max-wait-hours", "168",
    ]


def group_remaining(group_file, ledger):
    try:
        ids = json.load(open(os.path.join(ROOT, group_file), encoding="utf-8"))
    except Exception:
        return 0
    try:
        d = json.load(open(ledger, encoding="utf-8"))
    except Exception:
        return len(ids)
    remaining = 0
    for tid in ids:
        a = (d.get("tasks", {}).get(tid, {}).get("arms", {}).get("harnessmetric_hy3") or {})
        if a.get("status") == "infrastructure_failure":
            remaining += 1
    return remaining


def launch(worker_name, cfg):
    args = base_args()
    args += ["--treatment", cfg["treatment"], "--model", cfg["model"]]
    group_file = cfg["task_group"]
    ids = json.load(open(os.path.join(ROOT, group_file), encoding="utf-8"))
    for i in ids:
        args += ["--instance-id", i]
    env = dict(os.environ)
    python = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    stdout = open(os.path.join(RUN_ROOT, f"{worker_name}.supervised.stdout.log"), "a", encoding="utf-8")
    stderr = open(os.path.join(RUN_ROOT, f"{worker_name}.supervised.stderr.log"), "a", encoding="utf-8")
    proc = subprocess.Popen([python] + args, cwd=REPO, env=env,
                            stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
    with open(os.path.join(RUN_ROOT, f"{worker_name}.pid"), "w") as f:
        f.write(str(proc.pid))
    log(f"relaunched {worker_name} pid={proc.pid} ({len(ids)} tasks)")
    return proc.pid


def main():
    config = json.load(open(CONFIG, encoding="utf-8"))
    log("supervisor started")
    while True:
        try:
            for name, cfg in config.items():
                pid_file = os.path.join(RUN_ROOT, f"{name}.pid")
                if not os.path.exists(pid_file):
                    continue
                pid = open(pid_file).read().strip()
                if alive(pid):
                    continue
                remaining = group_remaining(cfg["task_group"], LEDGER)
                if remaining > 0:
                    log(f"{name} dead (pid {pid}); {remaining} infra tasks left -> relaunch")
                    launch(name, cfg)
                else:
                    log(f"{name} dead (pid {pid}); no infra tasks left, skip relaunch")
        except Exception as e:
            log(f"supervisor err: {e}")
        time.sleep(120)


if __name__ == "__main__":
    main()
