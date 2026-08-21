import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

RUN_LOG = []          # collected output lines (shared across clients)
RUN_LOCK = threading.Lock()
STDIN_LOCK = threading.Lock()

WORKER = {"proc": None, "ready": False, "running": False}

# Latest automation screenshot, written by the worker's live-capture thread
# (see _live_frame_paths in auto_challenge.py - keep the filename in sync).
LIVE_FRAME = os.path.join(tempfile.gettempdir(), "prize_automation_live.png")


def _script_path(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _ss_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ss")


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

    # The worker is spawned lazily on the first run and boots in a second or
    # two, printing READY before Chrome opens. With the startup optimizations
    # (cached patched chromedriver + persistent profile) the browser itself
    # opens in ~2s, so the first run proceeds automatically.
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
        payload = {"running": WORKER["running"], "ready": WORKER["ready"]}
    payload["live"] = os.path.exists(LIVE_FRAME)
    return jsonify(payload)


@app.route("/stop", methods=["POST"])
def stop():
    """Ask the worker to abort the current run and close the browser."""
    with RUN_LOCK:
        proc = WORKER["proc"]
    if proc is None or proc.poll() is not None:
        return jsonify({"error": "Nothing is running."}), 404
    with STDIN_LOCK:
        try:
            proc.stdin.write("STOP\n")
            proc.stdin.flush()
        except Exception:
            return jsonify({"error": "Worker is not responding."}), 500
    return jsonify({"stopped": True})


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


@app.route("/bills")
def bills():
    """List saved bill screenshots (oldest first) for the 'Your Bills' tab."""
    d = _ss_dir()
    if not os.path.isdir(d):
        return jsonify({"bills": []})
    files = [f for f in os.listdir(d) if f.lower().endswith(".png")]

    def _num(f):
        m = re.search(r"(\d+)", f)
        return int(m.group(1)) if m else 0

    files.sort(key=_num)
    return jsonify(
        {"bills": [{"file": f, "name": os.path.splitext(f)[0]} for f in files]}
    )


@app.route("/ss/<path:filename>")
def ss_file(filename):
    """Serve a screenshot from the ss folder."""
    return send_from_directory(_ss_dir(), filename)


@app.route("/live")
def live():
    """Stream the worker's live-view frames as MJPEG (PNG parts).

    The page's <img> element points here; Chrome plays the multipart stream
    natively, no JS needed. A part is only sent when the frame file's mtime
    changed, so an unchanged page costs no bandwidth.
    """
    def gen():
        last_stamp = None
        while True:
            try:
                stamp = os.path.getmtime(LIVE_FRAME)
                if stamp != last_stamp:
                    with open(LIVE_FRAME, "rb") as f:
                        data = f.read()
                    last_stamp = stamp
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/png\r\n"
                        b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                        + data + b"\r\n"
                    )
                    continue
            except OSError:
                # No live run right now (or file mid-replace); retry shortly.
                last_stamp = None
            time.sleep(0.15)

    return Response(
        gen(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


if __name__ == "__main__":
    # Chrome is NOT opened here - the worker opens it on each "Run Automation"
    # click and closes it as soon as the run + screenshot is done, so no Chrome
    # window lingers on the server between runs.
    port = int(os.environ.get("PORT", 8000))
    print(f"Listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
