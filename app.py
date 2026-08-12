import json
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

RUN_LOG = []          # collected output lines (shared across clients)
RUN_LOCK = threading.Lock()
STDIN_LOCK = threading.Lock()

WORKER = {"proc": None, "ready": False, "running": False}


def _script_path(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _read_worker(proc):
    """Drain worker stdout into RUN_LOG and track lifecycle sentinels."""
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if line == "READY":
            with RUN_LOCK:
                WORKER["ready"] = True
            continue
        if line == "RUN_DONE":
            with RUN_LOCK:
                WORKER["running"] = False
            continue
        RUN_LOG.append(line)
    # Worker process died.
    with RUN_LOCK:
        WORKER["ready"] = False
        WORKER["running"] = False


def _ensure_worker():
    """Spawn the persistent automation worker if it is not running."""
    with RUN_LOCK:
        proc = WORKER["proc"]
        if proc is not None and proc.poll() is None:
            return True
        script = _script_path("auto_challenge.py")
        proc = subprocess.Popen(
            [sys.executable, "-u", script, "--service"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        WORKER["proc"] = proc
        WORKER["ready"] = False
        WORKER["running"] = False
        threading.Thread(target=_read_worker, args=(proc,), daemon=True).start()
        return True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    """Send one run request to the persistent worker."""
    data = request.get_json(silent=True) or {}

    with RUN_LOCK:
        if WORKER["running"]:
            return jsonify({"error": "A run is already in progress."}), 409

    _ensure_worker()

    # First run after the server starts has to cold-start Chrome (can take
    # up to ~30s). Wait it out here so the run proceeds automatically.
    deadline = time.time() + 60
    while time.time() < deadline:
        with RUN_LOCK:
            if WORKER["ready"]:
                break
        time.sleep(0.2)
    with RUN_LOCK:
        if not WORKER["ready"]:
            return jsonify({"error": "Browser is still warming up. Try again in a few seconds."}), 503
        RUN_LOG.clear()
        WORKER["running"] = True

    with STDIN_LOCK:
        WORKER["proc"].stdin.write(json.dumps(data) + "\n")
        WORKER["proc"].stdin.flush()
    return jsonify({"started": True})


@app.route("/status")
def status():
    with RUN_LOCK:
        return jsonify(
            {"running": WORKER["running"], "ready": WORKER["ready"]}
        )


@app.route("/log")
def log():
    """Return output lines after `pos`, for client-side polling."""
    pos = request.args.get("pos", 0, type=int)
    if pos < 0:
        pos = 0
    with RUN_LOCK:
        lines = RUN_LOG[pos:]
        new_pos = len(RUN_LOG)
    return jsonify({"lines": lines, "pos": new_pos})


if __name__ == "__main__":
    # Chrome is NOT opened here anymore - it only launches when you click
    # "Run Automation" on the page.
    port = int(os.environ.get("PORT", 8000))
    print(f"Listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
