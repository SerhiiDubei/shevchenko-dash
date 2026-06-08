"""
Pod-side pusher: every 10s build a snapshot of the autoresearch pipeline and
POST it to the Railway dashboard. Dependency-free (urllib).

Env:
  DASH_URL     full ingest endpoint, e.g. https://xxx.up.railway.app/ingest
  DASH_SECRET  shared secret matching the server's INGEST_SECRET

Run:  DASH_URL=... DASH_SECRET=... python3 push.py
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

URL = os.environ["DASH_URL"]
SECRET = os.environ["DASH_SECRET"]
ROOT = Path("/workspace/shevchenko-ft")
HIST = ROOT / "outputs" / "history"
RUN = ROOT / "outputs" / "run"
RUNLOG = ROOT / "run.log"


def git(*a):
    try:
        return subprocess.check_output(["git"] + list(a), cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "")).strftime("%H:%M:%S")
    except Exception:
        return ""


def build():
    rows = []
    for mj in HIST.glob("*/metrics.json"):
        try:
            d = json.loads(mj.read_text())
        except Exception:
            continue
        m = d.get("metrics", {})
        msg = (d.get("commit_message", "") or "")
        rows.append({
            "commit": d.get("commit", mj.parent.name),
            "ts": d.get("timestamp", ""),
            "start": fmt_time(d.get("timestamp", "")),
            "rank": m.get("rank"),
            "eval_loss": m.get("eval_loss"),
            "train_sec": m.get("runtime_seconds"),
            "total_sec": m.get("total_seconds"),
            "crashed": d.get("crashed", False),
            "desc": msg.splitlines()[0] if msg else "",
        })
    rows.sort(key=lambda r: r["ts"])
    for i, r in enumerate(rows, 1):
        r["idx"] = i

    valid = [r for r in rows if not r["crashed"] and r["eval_loss"] is not None]
    best = min(valid, key=lambda r: r["eval_loss"]) if valid else None
    for r in rows:
        r["best"] = best is not None and r is best

    gpu = sum(r["train_sec"] or 0 for r in rows)
    wall = sum(r["total_sec"] or 0 for r in rows)
    summary = {
        "count": len(rows),
        "gpu_sec": gpu,
        "pipeline_sec": wall,
        "avg_sec": wall / len(rows) if rows else None,
        "best_eval": best["eval_loss"] if best else None,
        "best_commit": best["commit"] if best else None,
    }

    running = {"active": False}
    is_running = subprocess.call(["pgrep", "-f", "train_one.py"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    if is_running:
        step = tot = None
        if RUNLOG.exists():
            txt = RUNLOG.read_text(errors="ignore")
            ms = re.findall(r"(\d+)/(\d+) \[", txt)  # tqdm "17/60 ["
            if ms:
                step, tot = int(ms[-1][0]), int(ms[-1][1])
        elapsed = None
        try:
            pid = subprocess.check_output(["pgrep", "-f", "train_one.py"], text=True).split()[0]
            elapsed = float(subprocess.check_output(["ps", "-o", "etimes=", "-p", pid], text=True).strip())
        except Exception:
            pass
        running = {
            "active": True,
            "commit": git("rev-parse", "--short", "HEAD"),
            "desc": git("log", "-1", "--pretty=%s"),
            "step": step, "total_steps": tot, "elapsed_sec": elapsed,
        }

    samples = []
    sp = RUN / "samples.json"
    if sp.exists():
        try:
            samples = json.loads(sp.read_text())
        except Exception:
            pass
    if not samples:
        hs = sorted(HIST.glob("*/samples.json"), key=lambda p: p.stat().st_mtime)
        if hs:
            try:
                samples = json.loads(hs[-1].read_text())
            except Exception:
                pass

    return {
        "updated": datetime.utcnow().isoformat() + "Z",
        "experiments": rows, "summary": summary,
        "running": running, "samples": samples,
    }


def push(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        URL, data=data,
        headers={"Content-Type": "application/json", "x-secret": SECRET},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


if __name__ == "__main__":
    print(f"pusher -> {URL}", flush=True)
    while True:
        try:
            code = push(build())
            print(f"pushed {code} @ {datetime.utcnow().strftime('%H:%M:%S')}", flush=True)
        except Exception as e:
            print("push error:", repr(e), flush=True)
        time.sleep(10)
