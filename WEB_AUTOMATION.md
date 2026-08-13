# How the Web Automation Works (app.py)

The webpage (`app.py`) is just a thin Flask shell around the real automation in
`auto_challenge.py`. It does not run any browser automation itself — it spawns
`auto_challenge.py` as a background worker and streams its output to the page.

## High-level flow

```
Browser page ──POST /run──▶ Flask app ──spawns──▶ auto_challenge.py --service
      ▲                                                  │
      │   GET /status, /log                              │ reads a JSON run request
      │        (polling)                                 │ from stdin
      └──────────────────────────────────────────────────┘
                          worker does:
                          open Chrome → fill form → solve captcha → submit
```

1. You open `http://127.0.0.1:8000` and click **Run Automation**.
2. The page sends `POST /run` with a JSON body (currently `{}`).
3. Flask ensures the worker is running and writes the JSON to the worker's
   **stdin** — one JSON object per run.
4. The worker opens Chrome, runs the fill → captcha → submit cycle, then
   reports back so the page can update.

## Pieces

### 1. The worker process (`auto_challenge.py --service`)

`app.py:44` `_ensure_worker()` starts the persistent worker:

```python
subprocess.Popen([sys.executable, "-u", script, "--service"],
                 stdin=PIPE, stdout=PIPE, ...)
```

- Started **once** and reused for every run (no Python/import startup cost per
  run — this is why repeat runs feel fast).
- `-u` makes its output unbuffered so Flask reads lines as they happen.
- Its stdout is merged with stderr so all logs go through one pipe.

### 2. Request/response over stdin/stdout

The worker (`auto_challenge.py:618` `service_loop()`) speaks a tiny protocol:

| Direction | Message     | Meaning                                  |
|-----------|-------------|------------------------------------------|
| Flask →   | JSON line   | one run request; keys become `FORM_*` env vars |
| Worker →  | `READY`     | worker is up, waiting for work           |
| Worker →  | `RUN_DONE`  | current run finished                     |
| Worker →  | log lines   | everything else, appended to the log     |

`app.py:23` `_read_worker()` reads each stdout line: sentinels update the
worker state, everything else is stored in `RUN_LOG`.

### 3. Form data overrides

A run request's JSON keys are turned into env vars before the run
(`auto_challenge.py:655`):

```python
for key, value in req.items():
    os.environ["FORM_" + key.upper()] = str(value)
```

`auto_challenge.py:49` `build_form_data()` then fills the form from
`FORM_*` env vars, falling back to `DUMMY_FORM_DATA` for anything missing.
So the webpage could send `{"bill_number": "X", "name": "Y"}` and the worker
would use those values without editing any code.

### 4. What a run does (`auto_challenge.py:550` `run_once()`)

1. **Fill form** — sets every input in one JS pass (bulk fill) using native
   setters + `input`/`change` events so React-style validation fires, then
   verifies the values stuck (retries, falls back to one-by-one).
2. **Solve captcha** — Turnstile usually auto-passes. If not, it tries a real
   mouse click on the checkbox iframe inside the widget's shadow DOM.
3. **Submit** — waits for the submit button to unlock (only happens once the
   Turnstile token is granted), then clicks it.

### 5. Browser lifecycle

- Chrome is opened **per run** and closed after it (`KEEP_BROWSER_OPEN = False`
  in `auto_challenge.py:22`), so nothing lingers in the taskbar when idle.
- The worker is self-healing: if the browser dies mid-run it restarts Chrome
  and keeps serving (`auto_challenge.py:673`).

### 6. The page console

`templates/index.html` polls two endpoints:

- `GET /status` (`app.py:102`) — is a run in progress? is the worker ready?
- `GET /log?pos=N` (`app.py:110`) — fetch output lines after position `N`
  (offset-based polling, no resend of old lines).

Lines are styled in the console: `[+]` green, `[!]` yellow, `RESULT:` red.

## Why it's fast

- The worker process persists, so Python + undetected-chromedriver init
  happens once, not per run.
- On subsequent runs Chrome is cold-started only when `KEEP_BROWSER_OPEN` is
  off; `main()` in `auto_challenge.py` keeps the browser open across repeat
  runs for the same benefit when run directly.
- `FAST_MODE` scales the human-like delays down (see `auto_challenge.py:24`).

## Run it

```bash
python app.py          # start the server
# open http://127.0.0.1:8000 in a browser
```

Standalone, without the webpage:

```bash
python auto_challenge.py            # one run, reuse browser on repeats
python auto_challenge.py --service  # run the worker loop directly
```
