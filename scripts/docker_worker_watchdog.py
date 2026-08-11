# docker_worker_watchdog.py
# Watches Docker daemon and HM workers. Restarts Docker Desktop on outage.
# Logs to runs/hm100/watchdog.log. Run via Monitor tool (persistent).
import json
import os
import subprocess
import time
import datetime
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # AutoMetric/
LOG = os.path.join(ROOT, "runs", "hm100", "watchdog.log")
WORKERS = ("hm11_a", "hm11_b", "hm11_c")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def docker_ok():
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def restart_docker():
    subprocess.run(
        ["powershell", "-Command",
         "Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process -Force"],
        capture_output=True, timeout=30,
    )
    time.sleep(3)
    subprocess.run(
        ["powershell", "-Command",
         "Start-Process -FilePath 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'"],
        capture_output=True, timeout=30,
    )


def workers_alive():
    alive = []
    for name in WORKERS:
        try:
            pid_path = os.path.join(ROOT, "runs", "hm100", f"{name}.pid")
            pid = open(pid_path).read().strip()
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True)
            alive.append((name, pid in r.stdout))
        except Exception:
            alive.append((name, False))
    return alive


def hm_progress():
    ledger = os.path.join(ROOT, "runs", "hm100", "experiment.json")
    try:
        d = json.load(open(ledger, encoding="utf-8"))
    except Exception:
        return {}
    c = collections.Counter()
    for tid, t in d["tasks"].items():
        a = t["arms"].get("harnessmetric_hy3") or {}
        st = a.get("status")
        if st == "completed":
            c[("completed", a.get("resolved"))] += 1
        elif st == "censored":
            c[("censored", a.get("resolved"))] += 1
        elif st == "infrastructure_failure":
            c["infra"] += 1
    return dict(c)


down_since = None
log("watchdog started")
while True:
    if not docker_ok():
        if down_since is None:
            down_since = datetime.datetime.now()
            log(f"docker DOWN at {down_since.strftime('%H:%M:%S')}; restarting Docker Desktop")
            restart_docker()
        time.sleep(30)
        continue
    if down_since is not None:
        dur = (datetime.datetime.now() - down_since).total_seconds() / 60
        log(f"docker RECOVERED after {dur:.1f} min")
        down_since = None
    try:
        prog = hm_progress()
        wa = workers_alive()
        log(f"HM={prog} workers={wa}")
    except Exception as e:
        log(f"progress err: {e}")
    time.sleep(300)
