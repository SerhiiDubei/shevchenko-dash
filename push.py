"""
Pod-side pusher: every 10s build a snapshot of the autoresearch pipeline and
POST it to the Railway dashboard. Dependency-free (urllib).

Env:
  DASH_URL     full ingest endpoint
  DASH_SECRET  shared secret
  GPU_HOURLY   $/hr for the H100 (default 3.0) — for cost estimate
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
GPU_RATE = float(os.environ.get("GPU_HOURLY", "3.0"))
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


def quality_score(samples):
    """Cheap code-based style score (0-100) for generated poems.
    orth = old-orthography density (ѣ, ъ); rep = repeated-line penalty;
    contam = latin/garbage penalty. Comparable across configs, $0."""
    if not samples:
        return None
    txt = "\n".join(s if isinstance(s, str) else "" for s in samples)
    words = max(1, len(re.findall(r"\w+", txt)))
    archaic = len(re.findall(r"[ѣъ]", txt))  # ѣ (yat) + ъ (hard sign)
    orth = min(1.0, archaic / (words * 0.12))
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    rep = (1 - len(set(lines)) / len(lines)) if lines else 0
    latin = len(re.findall(r"[A-Za-z]", txt))
    contam = min(1.0, latin / words)
    score = 100 * (0.55 * orth + 0.45 * (1 - rep) - 0.35 * contam)
    return {"score": max(0, min(100, round(score))),
            "orth": round(orth * 100), "rep": round(rep * 100), "contam": round(contam * 100)}


def read_samples(path):
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return []


def build():
    rows = []
    for mj in HIST.glob("*/metrics.json"):
        try:
            d = json.loads(mj.read_text())
        except Exception:
            continue
        m = d.get("metrics", {})
        msg = (d.get("commit_message", "") or "")
        ev, tl = m.get("eval_loss"), m.get("train_loss")
        samp = read_samples(mj.parent / "samples.json")
        rows.append({
            "commit": d.get("commit", mj.parent.name),
            "ts": d.get("timestamp", ""),
            "start": fmt_time(d.get("timestamp", "")),
            "rank": m.get("rank"),
            "eval_loss": ev,
            "train_loss": tl,
            "gap": round(ev - tl, 4) if (ev is not None and tl is not None) else None,
            "vram_gb": round(m.get("peak_vram_mb", 0) / 1024, 1) if m.get("peak_vram_mb") else None,
            "train_sec": m.get("runtime_seconds"),
            "total_sec": m.get("total_seconds"),
            "config": d.get("config", {}),
            "quality": quality_score(samp),
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
    qs = [r["quality"]["score"] for r in rows if r.get("quality")]
    summary = {
        "count": len(rows),
        "gpu_sec": gpu,
        "pipeline_sec": wall,
        "avg_sec": wall / len(rows) if rows else None,
        "best_eval": best["eval_loss"] if best else None,
        "best_commit": best["commit"] if best else None,
        "gpu_cost": round(gpu / 3600 * GPU_RATE, 2),
        "best_quality": max(qs) if qs else None,
    }

    running = {"active": False}
    is_running = subprocess.call(["pgrep", "-f", "train_one.py"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    if is_running:
        step = tot = None
        if RUNLOG.exists():
            ms = re.findall(r"(\d+)/(\d+) \[", RUNLOG.read_text(errors="ignore"))
            if ms:
                step, tot = int(ms[-1][0]), int(ms[-1][1])
        elapsed = None
        try:
            pid = subprocess.check_output(["pgrep", "-f", "train_one.py"], text=True).split()[0]
            elapsed = float(subprocess.check_output(["ps", "-o", "etimes=", "-p", pid], text=True).strip())
        except Exception:
            pass
        running = {"active": True, "commit": git("rev-parse", "--short", "HEAD"),
                   "desc": git("log", "-1", "--pretty=%s"),
                   "step": step, "total_steps": tot, "elapsed_sec": elapsed}

    samples = read_samples(RUN / "samples.json")
    if not samples:
        hs = sorted(HIST.glob("*/samples.json"), key=lambda p: p.stat().st_mtime)
        if hs:
            samples = read_samples(hs[-1])

    return {"updated": datetime.utcnow().isoformat() + "Z",
            "experiments": rows, "summary": summary, "running": running,
            "samples": samples, "samples_quality": quality_score(samples)}


def push(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=data,
                                 headers={"Content-Type": "application/json", "x-secret": SECRET})
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
