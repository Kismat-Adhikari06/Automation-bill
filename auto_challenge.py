import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Windows consoles default to cp1252, which crashes when printing the site's
# Nepali page title. Force UTF-8 with a lossy fallback so logging can never
# kill a run (the web worker already decodes this pipe as text).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================
# CONFIGURATION
# ============================================================
TARGET_URL = "https://prize.ird.gov.np/"

# Chrome starts HEADLESS only when you ask for it. The default is a VISIBLE
# browser (the site's Turnstile captcha auto-passes reliably in headed mode).
#   python auto_challenge.py                  -> VISIBLE (default)
#   HEADLESS=true python auto_challenge.py    -> HEADLESS (macOS/Linux shell)
# On Windows CMD:
#   set HEADLESS=true && python auto_challenge.py
# On PowerShell:
#   $env:HEADLESS="true"; python auto_challenge.py
#
# The same rule applies when starting app.py: set HEADLESS before launching
# app.py and the worker subprocess will inherit it.
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() in ("1", "true", "yes", "on")
HIDE_WINDOW = False  # only used for visible/headful mode
KEEP_BROWSER_OPEN = False  # False = Chrome opens per run and closes right after the screenshot is saved (no lingering window)
# Runtime cache lives under %LOCALAPPDATA% (not OneDrive) so it survives and
# is cheap to hit: cached Chrome path/version + a patched chromedriver per
# Chrome major version + a dedicated persistent Chrome profile.
RUNTIME_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PrizeAutomation"
)
CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.json")
PROFILE_DIR = os.path.join(RUNTIME_DIR, "ChromeProfile")
# Screenshots of each submitted bill go here, numbered 1st ss, 2nd ss, ...
SS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ss")

FAST_MODE = True   # scale all human delays down; False = original speeds
BULK_FILL = True   # fill all form fields in one JS pass; False = one-by-one

CHECKBOX_TIMEOUT = 30  # seconds to wait for iframe / checkbox
SHORT_WAIT = 3         # seconds to wait per checkbox selector before moving on
TOKEN_WAIT = 5         # seconds to let token validation finish
AUTO_PASS_WAIT = 12    # seconds to wait for auto-pass before manual captcha fallback
PAGE_LOAD_TIMEOUT = 45 # seconds allowed for the target page to load (startup)
RESULT_WAIT = 10       # seconds to wait for a result/navigation after submitting
DEBUG = False          # print diagnostic DOM dumps when the iframe hunt fails

# Dummy data used to fill the prize-check form (replace with real data later).
# Field names come from the site's actual form (prize.ird.gov.np).
DUMMY_FORM_DATA = {
    "bill_number": "ABC123456789",       # mix of letters + numbers, max 40
    "seller_pan_no": "601234567",        # must be EXACTLY 9 digits
    "billed_total_amount": "2500.50",    # numeric
    "name": "Ram Bahadur Thapa",         # 2-120 characters
    "mobile_number": "9841234567",       # 7-15 digits
    "address": "Kathmandu, Nepal",       # 5-250 characters
}


# ============================================================
# Helpers
# ============================================================

def build_form_data():
    """Form data for the current run.

    Defaults to DUMMY_FORM_DATA; the web app overrides individual fields
    via FORM_* env vars (FORM_BILL_NUMBER, FORM_SELLER_PAN_NO, ...) so the
    webpage can drive the automation without touching this file.
    """
    data = dict(DUMMY_FORM_DATA)
    for key in data:
        env_val = os.environ.get("FORM_" + key.upper())
        if env_val:
            data[key] = env_val
    return data


def human_delay(low=1.5, high=3.5):
    """Mimic a human reaction pause before an interaction.

    Timing stays random (uniform), but in FAST_MODE the whole range is
    scaled down by 0.2. Uniform 0.05s delays are a bigger bot tell than
    short variable ones, so we keep some jitter even when fast.
    """
    if FAST_MODE:
        low, high = low * 0.2, high * 0.2
    time.sleep(random.uniform(low, high))


def find_challenge_iframe(driver, timeout=SHORT_WAIT):
    """Return the security iframe element or None.

    Fast-fails after ~timeout seconds. On this site the Turnstile widget
    usually auto-passes (no iframe is ever created), so stalling the full
    CHECKBOX_TIMEOUT here just wastes ~30s looking for something that
    isn't there. The submit button becoming enabled is the real signal.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CLASS_NAME, "turnstile-widget"))
        )
        print("[*] Turnstile widget container found.")
    except Exception:
        pass

    keyword_xpath = (
        "//iframe[contains(@src, 'cloudflare') "
        "or contains(@src, 'turnstile') "
        "or contains(@src, 'captcha') "
        "or contains(@src, 'recaptcha') "
        "or contains(@src, 'hcaptcha') "
        "or contains(@id, 'turnstile') "
        "or contains(@class, 'turnstile')]"
    )
    shadow_script = (
        "const kw=['cloudflare','turnstile','captcha','recaptcha','hcaptcha'];"
        "const out=[];"
        "const walk=(root)=>{"
        "  for(const f of root.querySelectorAll('iframe')){"
        "    const s=(f.src||'').toLowerCase();"
        "    const id=(f.id||'').toLowerCase();"
        "    const cls=(f.className||'').toLowerCase();"
        "    if(kw.some(k=>s.includes(k)||id.includes(k)||cls.includes(k)))out.push(f);"
        "  }"
        "  for(const el of root.querySelectorAll('*')){"
        "    if(el.shadowRoot) walk(el.shadowRoot);"
        "  }"
        "};"
        "walk(document);"
        "return out[0] ? out[0] : null;"
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return driver.find_element(By.XPATH, keyword_xpath)
        except Exception:
            pass
        try:
            found = driver.execute_script(shadow_script)
            if found:
                return found
        except Exception:
            pass
        time.sleep(0.5)

    if DEBUG:
        print("[!] No challenge iframe found. Diagnostic dump:")
        try:
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                print(f"    - src={frame.get_attribute('src')}")
        except Exception:
            pass
    return None


def is_ticked(driver, element):
    """Return True if the Turnstile checkbox currently reads as ticked."""
    return bool(driver.execute_script(
        "const el = arguments[0];"
        "return el.checked"
        "  || el.getAttribute('aria-checked') === 'true'"
        "  || (el.getAttribute('class') || '').includes('checked');",
        element,
    ))


def click_checkbox(driver):
    """Locate, click, and VERIFY the one-click checkbox inside the iframe.

    Each selector only waits SHORT_WAIT seconds, so if the checkbox is
    absent we fail fast and move on instead of stalling for 30s per try.
    After clicking we poll until the tick actually sticks - Turnstile
    often re-renders and untick if the click lands while it is still
    loading, which would leave the submit button locked.
    """
    selectors = [
        "//input[@type='checkbox']",
        "//div[@role='checkbox']",
        "//div[@id='challenge-stage']//*[contains(@class, 'ctp-checkbox')]",
        "//div[@id='challenge-stage']//*[contains(@class, 'checkbox')]",
        "//div[@id='challenge-stage']",
    ]
    for selector in selectors:
        try:
            element = WebDriverWait(driver, SHORT_WAIT).until(
                EC.element_to_be_clickable((By.XPATH, selector))
            )
            human_delay()
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element
            )
            human_delay(0.4, 1.2)
            element.click()

            try:
                WebDriverWait(driver, SHORT_WAIT).until(
                    lambda d: is_ticked(d, element)
                )
                return True
            except Exception:
                # Tick didn't stick (widget was still loading). Try once more.
                print("[!] Tick did not stick; retrying checkbox click.")
                human_delay()
                element.click()
                WebDriverWait(driver, SHORT_WAIT).until(
                    lambda d: is_ticked(d, element)
                )
                return True
        except Exception:
            continue
    return False


def _all_fields_present(driver, names):
    """True when every form field is currently in the DOM."""
    try:
        found = driver.find_elements(
            By.CSS_SELECTOR,
            ", ".join(f"input[name='{n}']" for n in names),
        )
        return len(found) >= len(names)
    except Exception:
        return False


def _values_ok(driver, names, values):
    """Return number of fields whose current value differs from expected."""
    return driver.execute_script(
        "const names = arguments[0], values = arguments[1];"
        "let bad = 0;"
        "names.forEach((n, i) => {"
        "  const el = document.querySelector(`input[name='${n}']`);"
        "  if (!el || el.value !== values[i]) bad++;"
        "});"
        "return bad;",
        names,
        values,
    )


def fill_form(driver, data):
    """Fill the prize-check form inputs.

    In BULK_FILL mode every field is set in a single JS pass (native value
    setter + input/change events, so React-style validation still fires).
    The page can render the form late (especially the first load after the
    server starts), so we wait for ALL fields to exist, fill, then VERIFY
    the values actually stuck - retrying if a re-render wiped them.

    Returns the number of fields successfully filled (out of len(data)).
    """
    fields = list(data.items())
    if not BULK_FILL:
        return _fill_form_human(driver, fields)

    names = [n for n, _ in fields]
    values = [v for _, v in fields]
    script = (
        "const names = arguments[0];"
        "const values = arguments[1];"
        "const set = (el, v) => {"
        "  const proto = el instanceof HTMLTextAreaElement"
        "    ? HTMLTextAreaElement.prototype"
        "    : el instanceof HTMLSelectElement"
        "      ? HTMLSelectElement.prototype"
        "      : HTMLInputElement.prototype;"
        "  const desc = Object.getOwnPropertyDescriptor(proto, 'value');"
        "  desc.set.call(el, v);"
        "  el.dispatchEvent(new Event('input', {bubbles: true}));"
        "  el.dispatchEvent(new Event('change', {bubbles: true}));"
        "};"
        "const missing = [];"
        "names.forEach((n, i) => {"
        "  const el = document.querySelector(`input[name='${n}']`);"
        "  if (el) set(el, values[i]); else missing.push(n);"
        "});"
        "return missing;"
    )

    try:
        WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
            lambda d: _all_fields_present(d, names)
        )
    except Exception:
        print("[!] Form fields did not all appear; falling back to one-by-one.")
        return _fill_form_human(driver, fields)

    time.sleep(0.3)  # let the page settle before writing
    for attempt in range(1, 4):
        try:
            missing = driver.execute_script(script, names, values)
            bad = _values_ok(driver, names, values)
        except Exception as e:
            print(f"[!] Bulk fill attempt {attempt} error: {e}")
            bad, missing = 99, ["error"]
        if not missing and bad == 0:
            for name, value in fields:
                print(f"[+] Filled {name}: {value}")
            return len(fields)
        print(f"[!] Fill attempt {attempt} didn't stick ({bad} mismatched); retrying.")
        time.sleep(0.5)

    print("[!] Bulk fill still failing; falling back to one-by-one.")
    return _fill_form_human(driver, fields)


def _fill_form_human(driver, fields):
    """Original one-field-at-a-time fill with human-paced delays."""
    filled = 0
    for field_name, field_value in fields:
        try:
            field = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"input[name='{field_name}']")
                )
            )
            human_delay()
            field.clear()
            field.send_keys(field_value)
            print(f"[+] Filled {field_name}: {field_value}")
            filled += 1
        except Exception:
            print(f"[!] Could not find input field: {field_name}")
    return filled


SUBMIT_XPATH = (
    "//button[normalize-space(.)='Generate prize coupon' "
    "or normalize-space(.)='पुरस्कार कुपन सिर्जना गर्नुहोस्'] | "
    "//button[@type='submit'][contains(@class, 'primary-button')]"
)


def _click_widget_checkbox(driver):
    """Click the Turnstile checkbox via a real mouse click on its iframe.

    Some widget layouts put the checkbox inside an iframe whose src does
    not contain 'cloudflare'/'turnstile' keywords, so the keyword search
    misses it. We locate any iframe inside the widget's shadow root and
    click its center like a human would.
    """
    widget = WebDriverWait(driver, SHORT_WAIT).until(
        EC.presence_of_element_located((By.CLASS_NAME, "turnstile-widget"))
    )
    frame = driver.execute_script(
        "const root = arguments[0].shadowRoot || arguments[0];"
        "if (!root) return null;"
        "const f = root.querySelector('iframe');"
        "if (f) return f;"
        "for (const c of root.querySelectorAll('*')) {"
        "  if (c.shadowRoot) {"
        "    const i = c.shadowRoot.querySelector('iframe');"
        "    if (i) return i;"
        "  }"
        "}"
        "return null;",
        widget,
    )
    if not frame:
        return False

    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", frame
    )
    human_delay(0.4, 1.2)
    ActionChains(driver).move_to_element(frame).click().perform()
    return True


def _manual_captcha_fallback(driver):
    """Replicate the manual checkbox click a human would do.

    Used when the captcha fails to auto-pass. Tries (in order):
    1. the challenge iframe (incl. shadow DOM) with tick verification,
    2. a real mouse click on the widget's checkbox iframe,
    3. a direct JS click on a shadow-DOM checkbox.
    """
    iframe = find_challenge_iframe(driver, timeout=8)
    if iframe is not None:
        try:
            driver.switch_to.frame(iframe)
            clicked = click_checkbox(driver)
        finally:
            driver.switch_to.default_content()
        if clicked:
            print("[+] Manual checkbox click (iframe) succeeded.")
            return
        print("[!] Checkbox not found in fallback iframe.")

    try:
        if _click_widget_checkbox(driver):
            print("[+] Clicked Turnstile checkbox (real mouse click).")
            return
    except Exception as e:
        print(f"[!] Widget click failed: {e}")

    # Last resort: JS-click whatever checkbox we can reach in shadow DOM.
    try:
        widget = WebDriverWait(driver, SHORT_WAIT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "turnstile-widget"))
        )
        shadow_click = (
            "const el = arguments[0];"
            "const find = (root) => {"
            "  if (!root || !root.querySelector) return null;"
            "  const chk = root.querySelector("
            "    'input[type=checkbox], [role=checkbox], .ctp-checkbox');"
            "  if (chk) return chk;"
            "  for (const c of root.querySelectorAll('*')) {"
            "    if (c.shadowRoot) { const f = find(c.shadowRoot); if (f) return f; }"
            "  }"
            "  return null;"
            "};"
            "const cb = find(el.shadowRoot || el);"
            "if (cb) { cb.click(); return true; }"
            "return false;"
        )
        clicked = bool(driver.execute_script(shadow_click, widget))
        if clicked:
            human_delay()
            print("[+] Clicked Turnstile checkbox via JS (shadow DOM).")
    except Exception as e:
        print(f"[!] No Turnstile widget found to click: {e}")


def _aborted(abort_event):
    """True when an abort (Stop) has been requested."""
    return abort_event is not None and abort_event.is_set()


def _wait_until(predicate, timeout, poll=0.5, abort_event=None):
    """Poll predicate() until True, timeout, or abort; True only on success.

    The short poll lets an abort_event interrupt long waits (e.g. the
    captcha auto-pass wait) instead of blocking for the full timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _aborted(abort_event):
            return False
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def _turnstile_widget_present(driver):
    """True when a Turnstile widget container exists in the DOM."""
    try:
        return len(driver.find_elements(By.CLASS_NAME, "turnstile-widget")) > 0
    except Exception:
        return False


def _turnstile_token_granted(driver):
    """True when a Turnstile response token has been written to the page.

    Turnstile writes its token into a hidden input named
    cf-turnstile-response once verification succeeds. This is read-only
    diagnostics - it never forges or injects a token.
    """
    try:
        token = driver.find_element(
            By.CSS_SELECTOR, "input[name='cf-turnstile-response']"
        ).get_attribute("value")
        return bool(token and token.strip())
    except Exception:
        return False


def click_submit(driver, abort_event=None):
    """Wait for the submit button to become enabled, then click it.

    The site keeps the button disabled until the Turnstile token is
    granted. Most runs auto-pass within ~13s; if the button hasn't
    unlocked after AUTO_PASS_WAIT, we fall back to clicking the visible
    Turnstile checkbox manually, then keep waiting for the unlock.

    All enable-waits poll in short slices so a Stop request can interrupt
    a long captcha wait instead of blocking until the timeout.
    """
    submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
        EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
    )
    print("[*] Waiting for submit button to unlock...")
    if not _wait_until(lambda: submit_button.is_enabled(), AUTO_PASS_WAIT, abort_event=abort_event):
        if _aborted(abort_event):
            print("[!] Run aborted while waiting for captcha.")
            return False
        print("[!] Captcha didn't auto-pass; trying manual fallback click.")
        _manual_captcha_fallback(driver)
        submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
        )
        if not _wait_until(lambda: submit_button.is_enabled(), CHECKBOX_TIMEOUT, abort_event=abort_event):
            if _aborted(abort_event):
                print("[!] Run aborted while waiting for captcha.")
                return False
            # Still locked - try the manual click once more before giving up.
            print("[!] Still locked; retrying captcha click.")
            _manual_captcha_fallback(driver)
            submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
                EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
            )

    if not _wait_until(lambda: submit_button.is_enabled(), CHECKBOX_TIMEOUT, abort_event=abort_event):
        if _aborted(abort_event):
            print("[!] Run aborted while waiting for captcha.")
            return False
        mode = "HEADLESS" if HEADLESS else "VISIBLE"
        token = _turnstile_token_granted(driver)
        print(f"[!] FAIL: Turnstile verification not granted in {mode} mode "
              f"(token present={token}); submit button stayed disabled for "
              f"{CHECKBOX_TIMEOUT}s.")
        return False

    print("[+] Turnstile token granted; submit button enabled (captcha passed).")

    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", submit_button
    )
    human_delay(0.4, 1.2)

    if _aborted(abort_event):
        print("[!] Run aborted before clicking submit.")
        return False

    try:
        submit_button.click()
        print("[+] SUBMIT BUTTON CLICKED - success.")
        return True
    except Exception:
        driver.execute_script(
            "arguments[0].click();", submit_button
        )
        print("[+] SUBMIT BUTTON CLICKED via JS fallback - success.")
        return True


def _post_submit_check(driver):
    """Report what happened after clicking submit: 'ok'|'failed'|'unknown'.

    'ok'     - the page navigated to a different URL (result rendered).
    'failed' - the page surfaced field/validation errors.
    'unknown'- neither happened within RESULT_WAIT (submission was sent but
               the outcome could not be confirmed locally).
    """
    start_url = None
    try:
        start_url = driver.current_url
    except Exception:
        return "unknown"
    deadline = time.time() + RESULT_WAIT
    while time.time() < deadline:
        try:
            url = driver.current_url
            if url and url != start_url and "about:blank" not in url:
                return "ok"
        except Exception:
            pass
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".field-error, [role='alert']"):
                return "failed"
        except Exception:
            pass
        time.sleep(0.5)
    return "unknown"


def _chrome_candidates():
    cands = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Chrome\Application\chrome.exe",
        # Linux (Docker / VPS deploys) and macOS
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            p = shutil.which(name)
            if p:
                cands.append(p)
        except Exception:
            pass
    return [c for c in cands if c]


CHROME_CANDIDATES = _chrome_candidates()


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg):
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[!] Could not write config cache: {_safe(str(e))}")


def _detect_chrome_version(path):
    """Major Chrome version (e.g. 151) from the installed binary.

    Cache-miss only. Windows: reads the file's version info via PowerShell -
    launches nothing, touches no registry. Linux/macOS: `--version` is a
    fast one-liner that prints and exits.
    """
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-Item -LiteralPath '%s').VersionInfo.FileVersion" % path],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            m = re.search(r"\b(\d+)\.", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        m = re.search(r"\b(\d+)\.", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def resolve_chrome():
    """Return (chrome_exe_path, major_version) using a local cache.

    The cache stores the exact path plus the file's mtime, so a hit is a
    single stat call - no registry queries and no disk re-scan on every run.
    If the cached file is gone or its mtime changed (Chrome updated), the
    path is re-detected and the version read once, then re-cached.
    """
    cfg = _load_config()
    cached = cfg.get("chrome_path")
    if cached and os.path.isfile(cached):
        try:
            if os.path.getmtime(cached) == cfg.get("chrome_mtime"):
                return cached, cfg.get("chrome_major")
        except Exception:
            pass

    path = None
    for cand in CHROME_CANDIDATES:
        if os.path.isfile(cand):
            path = cand
            break
    if not path:
        print("[!] Chrome executable not found; falling back to uc detection.")
        return None, None

    version = _detect_chrome_version(path)
    if version is None:
        print(f"[!] Could not read Chrome version from {path}; falling back to uc detection.")
        return path, None
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None
    _save_config({"chrome_path": path, "chrome_major": version, "chrome_mtime": mtime})
    print(f"[*] Chrome: {path} (v{version}) - cached for future runs.")
    return path, version


def _is_patched(path):
    """True if the file looks like an undetected_chromedriver-patched driver.

    uc embeds its marker late in the binary (it survived an integrity check
    patch), so scan the whole file rather than just the header.
    """
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return b"undetected chromedriver" in f.read()
    except Exception:
        return False


def get_patched_driver(major):
    """Return a cached, already-patched chromedriver path for this Chrome major version.

    undetected_chromedriver unlinks its own cached driver and re-downloads
    + re-patches it from Google's servers on every fresh launch - that is
    the main startup cost. We keep one patched copy per Chrome major version
    in %LOCALAPPDATA%\\PrizeAutomation and hand it to uc via
    driver_executable_path, which makes uc treat it as a ready custom driver
    and skip the download entirely. It is built (network) only once per
    Chrome version.
    """
    if not major:
        return None
    target = os.path.join(RUNTIME_DIR, f"chromedriver_{major}.exe")
    if _is_patched(target):
        return target
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        patcher = uc.Patcher(version_main=major)
        patcher.auto()
        src = patcher.executable_path
        if src and os.path.isfile(src) and _is_patched(src):
            shutil.copy2(src, target)
            print(f"[*] Patched chromedriver cached at {target}")
            return target
    except Exception as e:
        print(f"[!] Could not build patched chromedriver: {_safe(str(e))}")
    return None


TIMINGS = {}


def _reset_timings():
    TIMINGS.clear()
    TIMINGS["t0"] = time.perf_counter()


def _print_startup_timings():
    t = TIMINGS
    if "ready_done" not in t:
        return

    def d(a, b):
        if a in t and b in t:
            return t[b] - t[a]
        return None

    def fmt(x):
        return f"{x:.2f}s" if x is not None else "-"

    print("[timing] Python startup -> create_driver():        " + fmt(d("t0", "create_driver_start")))
    print("[timing] Chrome executable resolution (cached):    " + fmt(d("create_driver_start", "executable_done")))
    print("[timing] Patched driver cache:                     " + fmt(d("executable_done", "driver_cache_done")))
    print("[timing] WebDriver init (uc.Chrome):               " + fmt(d("driver_cache_done", "webdriver_done")))
    print("[timing] Target navigation (driver.get):           " + fmt(d("webdriver_done", "nav_done")))
    print("[timing] Page ready wait:                          " + fmt(d("nav_done", "ready_done")))
    print("[timing] TOTAL startup (script -> page ready):     " + fmt(d("t0", "ready_done")))


def _safe(text):
    """Render text without risking console-encoding crashes (non-ASCII -> \\u escapes)."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _page_ready(driver):
    """True when the driver has navigated away from a blank tab."""
    try:
        url = driver.current_url
        return bool(url and "about:blank" not in url)
    except Exception:
        return False


def _chrome_pids():
    """PIDs of every running Chrome process (used to tell if any Chrome remains)."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\").ProcessId"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        else:
            out = subprocess.run(
                ["pgrep", "-x", "chrome"], capture_output=True, text=True, timeout=15,
            ).stdout
        return [int(t) for t in out.split() if t.strip().isdigit()]
    except Exception:
        return []


def _automation_chrome_pids():
    """PIDs of Chrome instances running with OUR dedicated profile.

    This identifies exactly the Chrome that does the automation (its command
    line contains the PrizeAutomation profile dir), so a close/force-kill
    never touches the user's own Chrome.
    """
    pids = []
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
                  "| Where-Object { $_.CommandLine -match 'PrizeAutomation' } "
                  "| Select-Object -ExpandProperty ProcessId")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            ).stdout
        else:
            out = subprocess.run(
                ["pgrep", "-f", "PrizeAutomation"], capture_output=True, text=True, timeout=15,
            ).stdout
        for tok in out.split():
            tok = tok.strip()
            if tok.isdigit():
                pids.append(int(tok))
    except Exception:
        pass
    return pids


def _force_close_chrome(pids):
    """Hard-kill the given Chrome processes (tree) - only used as a fallback."""
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                subprocess.run(
                    ["kill", "-9", str(pid)], capture_output=True, text=True, timeout=15,
                )
        except Exception:
            pass


def close_automation_browser(driver):
    """Close the automation's own Chrome (the one that took the screenshot).

    1) Normal WebDriver quit() first.
    2) If Chrome is still running with our profile, force-kill just those
       processes - the user's own Chrome is never touched.
    3) If no Chrome remains at all afterwards, the automation is fully stopped.
    """
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    leftover = _automation_chrome_pids()
    if leftover:
        print(f"[*] Automation Chrome still alive (PIDs {leftover}); force-closing it.")
        _force_close_chrome(leftover)
    if not _chrome_pids():
        print("[*] No Chrome remaining; automation stopped.")


def create_driver():
    """Build the undetected Chrome driver with stability-focused flags.

    HEADLESS is driven by the HEADLESS env var (see CONFIGURATION); visible
    mode stays the default fallback.

    Startup optimizations:
      * Chrome binary and its major version are resolved from a local cache
        (no registry queries, no disk re-scan on every run).
      * A patched chromedriver is cached per Chrome major version and passed
        via driver_executable_path so uc skips its per-launch download+patch.
      * A dedicated persistent profile under %LOCALAPPDATA% is reused, so a
        warm session restarts much faster than a fresh temporary profile.

    How undetected_chromedriver handles headless: uc strips any `--headless*`
    argument out of the options object and re-adds the modern `--headless=new`
    flag itself (or `--headless=chrome` for Chrome < 108) whenever headless is
    enabled - either via the `headless=True` kwarg or the options object. We
    use both, then log the exact launch arguments and confirm headlessness on
    the running instance so the effective mode is verifiable in the logs.
    """
    TIMINGS["create_driver_start"] = time.perf_counter()
    chrome_path, version = resolve_chrome()
    TIMINGS["executable_done"] = time.perf_counter()

    options = uc.ChromeOptions()
    if chrome_path:
        # Point uc at the exact binary so it never does its own (registry-based)
        # executable hunt.
        options.binary_location = chrome_path
    if HEADLESS:
        options.add_argument("--headless=new")
        # Headless has no real screen, so give it an explicit viewport so the
        # page renders with a normal layout size.
        options.add_argument("--window-size=1920,1080")
        print("[*] Chrome mode: HEADLESS (no visible window)")
    else:
        print("[*] Chrome mode: VISIBLE")

    # Don't wait for all background scripts (Cloudflare/Turnstile) to finish
    # loading before starting; our WebDriverWait calls handle readiness.
    options.page_load_strategy = "eager"

    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-infobars")
    if not HEADLESS and HIDE_WINDOW:
        # Keep a real, rendering browser (the captcha needs it) but position
        # the window off-screen so the user never sees it. Off-screen does
        # NOT throttle timers the way minimize/hidden tabs do.
        options.add_argument("--window-position=-32000,-32000")
        options.add_argument("--window-size=1920,1080")
    elif not HEADLESS:
        options.add_argument("--start-maximized")

    kwargs = {
        "options": options,
        # Persistent dedicated profile (local, not OneDrive): a warm session
        # reuses cookies/site data and boots noticeably faster.
        "user_data_dir": PROFILE_DIR,
    }
    if version is not None:
        kwargs["version_main"] = version
    # Cached patched driver: skips uc's per-launch unlink + network download.
    patched = get_patched_driver(version) if version else None
    TIMINGS["driver_cache_done"] = time.perf_counter()
    if patched:
        kwargs["driver_executable_path"] = patched
    elif version is not None:
        # No cache (first run for this Chrome version): uc builds the driver
        # itself, which is the one slow, network-dependent startup.
        print("[*] No cached patched chromedriver; building one (first run only).")
    if HEADLESS:
        # uc's documented switch. The explicit option above is redundant but
        # harmless - uc normalizes either into the modern headless flag.
        kwargs["headless"] = True

    driver = uc.Chrome(**kwargs)
    TIMINGS["webdriver_done"] = time.perf_counter()
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    # Log the exact args uc launched Chrome with. The browser process is
    # started from options.arguments, so a --headless=new here means the
    # flag reached the real Chrome instance (headlessness is then confirmed
    # on the live session in start_driver, after the page loads).
    print("[*] Chrome launch args: " + ", ".join(options.arguments))
    return driver


def start_driver():
    """Create a driver and load the page, retrying up to 3 times.

    Chrome sometimes opens and instantly closes on the first try
    (locked user-data-dir, stale driver cache, etc.). A fresh attempt
    usually fixes it without user intervention.
    """
    last_err = None
    TIMINGS.setdefault("t0", time.perf_counter())
    for attempt in range(1, 4):
        driver = None
        try:
            driver = create_driver()
            driver.get(TARGET_URL)
            TIMINGS["nav_done"] = time.perf_counter()
            # Confirm the target page actually rendered before returning so
            # later failures aren't blamed on form/captcha logic.
            if not _wait_until(lambda: _page_ready(driver), PAGE_LOAD_TIMEOUT):
                raise TimeoutError(
                    f"Target page did not load within {PAGE_LOAD_TIMEOUT}s"
                )
            TIMINGS["ready_done"] = time.perf_counter()
            _print_startup_timings()
            print(f"[+] Page loaded: {driver.current_url} (title: {_safe(driver.title)!r})")
            if HEADLESS:
                # Confirm headlessness on the live session used for this run.
                try:
                    ua = driver.execute_script("return navigator.userAgent")
                    if "Headless" in ua:
                        print("[+] Headless CONFIRMED on the running Chrome instance.")
                    else:
                        print(f"[!] WARNING: instance does NOT look headless (UA={ua!r}).")
                except Exception as e:
                    print(f"[!] Could not verify headless mode: {_safe(str(e))}")
            return driver
        except Exception as e:
            last_err = e
            print(f"[!] Startup attempt {attempt}/3 failed: {e}")
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            time.sleep(2)
    raise RuntimeError("Could not start the browser after 3 attempts") from last_err


# ============================================================
# Main flow
# ============================================================

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n):
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11 -> '11th', 21 -> '21st'."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIXES.get(n % 10, 'th')}"


def next_ss_path():
    """Path of the next numbered screenshot, e.g. ...\\ss\\3rd ss.png.

    Numbers only count files already named like 'Nth ss.png' (older
    timestamp-named screenshots are ignored), so numbering starts at
    '1st ss' regardless of what else is in the folder.
    """
    os.makedirs(SS_DIR, exist_ok=True)
    n = 1
    try:
        nums = []
        for f in os.listdir(SS_DIR):
            m = re.match(r"(\d+)(?:st|nd|rd|th)\s+ss\.png$", f, re.IGNORECASE)
            if m:
                nums.append(int(m.group(1)))
        n = max(nums) + 1 if nums else 1
    except Exception:
        n = 1
    return os.path.join(SS_DIR, f"{_ordinal(n)} ss.png")


def take_screenshot(driver):
    """Capture the current page into the ss folder (1st ss.png, 2nd ss.png, ...)."""
    try:
        path = next_ss_path()
        if driver.save_screenshot(path):
            print(f"[+] Screenshot saved: {path}")
            return path
    except Exception as e:
        print(f"[!] Screenshot failed: {_safe(str(e))}")
    return None


def run_once(driver, abort_event=None):
    """Execute one full fill -> captcha -> submit cycle on an open driver.

    If abort_event is set, the run bails out before each step so the web
    app's Stop button can halt a run mid-flight.
    """
    human_delay()

    if _aborted(abort_event):
        print("[!] Run aborted.")
        return False

    print("[1/3] Filling form...")
    data = build_form_data()
    filled = fill_form(driver, data)
    if filled == len(data):
        print(f"[+] Form filled successfully: {filled}/{len(data)} fields.")
    else:
        print(f"[!] Form fill incomplete: {filled}/{len(data)} fields.")

    if _aborted(abort_event):
        print("[!] Run aborted after form fill.")
        return False

    print("[2/3] Solving captcha...")
    if _turnstile_widget_present(driver):
        print("[*] Turnstile widget appeared on the page.")
    else:
        print("[*] Turnstile widget not found (may not have loaded yet).")
    # Best-effort checkbox click. The captcha may already auto-pass,
    # so a miss here must NOT abort the flow.
    iframe = find_challenge_iframe(driver)
    if iframe is not None:
        driver.switch_to.frame(iframe)
        if click_checkbox(driver):
            print("[+] Captcha ticked and confirmed.")
        else:
            print("[!] Checkbox not found inside iframe; continuing.")
        driver.switch_to.default_content()
    else:
        print("[!] No challenge iframe found; assuming captcha auto-passes.")

    if _aborted(abort_event):
        print("[!] Run aborted before submit.")
        return False

    print("[3/3] Submitting...")
    # Automatically submit once the captcha token unlocks the button.
    # click_submit() handles all waiting for the token/button.
    clicked = click_submit(driver, abort_event=abort_event)

    if _aborted(abort_event):
        print("[!] Run aborted after submit.")
        return False

    # Pause briefly for the result page to render.
    time.sleep(TOKEN_WAIT if not FAST_MODE else 3)

    if not clicked:
        print("[!] RESULT: SUBMISSION FAILED (submit button was never clicked).")
        return False

    state = _post_submit_check(driver)
    if state == "ok":
        print(f"[+] Submission succeeded: navigated to {driver.current_url!r}")
    elif state == "failed":
        print("[!] Submission failed: the page showed field/validation errors.")
    else:
        print(f"[!] Submission sent, but no navigation confirmed within {RESULT_WAIT}s.")

    take_screenshot(driver)

    print("=" * 50)
    if state == "failed":
        print("[!] RESULT: SUBMISSION FAILED.")
    else:
        print("[+] RESULT: BUTTON WAS CLICKED SUCCESSFULLY.")
    print("=" * 50)
    return state != "failed"


def main():
    """Run one automation cycle: open Chrome, run, save screenshot, close.

    Chrome closes as soon as the run finishes and the screenshot is saved to
    the ss folder, so nothing lingers on screen. The terminal then offers
    another run (which opens a fresh Chrome); typing 'q' or closing stdin
    (EOF) exits.
    """
    driver = None
    _reset_timings()
    try:
        while True:
            if driver is None:
                driver = start_driver()
            try:
                run_once(driver)
            except Exception as e:
                print(f"[!] Run failed: {e}")
            finally:
                # Close the automation's Chrome right after the run
                # (screenshot already saved to the ss folder).
                close_automation_browser(driver)
                driver = None
                print("[*] Chrome closed after run.")

            if HEADLESS or os.environ.get("WEB_MODE"):
                # Nothing to look at (headless) or the web app owns the console.
                print("[*] Run finished.")
                break

            try:
                again = input("[*] Press Enter to run again or 'q' to quit: ")
            except EOFError:
                print("[*] Run finished.")
                break
            if again.strip().lower() in ("q", "quit", "exit"):
                print("[*] Run finished.")
                break
    finally:
        close_automation_browser(driver)


def _recreate_driver(driver):
    """Safely tear down a dead driver and start a fresh one."""
    close_automation_browser(driver)
    return start_driver()


def _alive(driver):
    """True if the driver still has a live browser session."""
    try:
        driver.current_url
        return True
    except Exception:
        return False


def _stdin_reader(req_queue, stop_event):
    """Background thread feeding the service loop.

    Run requests (JSON lines) go to req_queue. "STOP" sets stop_event
    immediately - so a run mid-fill/mid-captcha can abort - and is queued
    too so the main loop can finalize cleanup. "QUIT" just queues.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "STOP":
            stop_event.set()
            req_queue.put("STOP")
        elif line == "QUIT":
            req_queue.put("QUIT")
        else:
            req_queue.put(line)


def service_loop():
    """Persistent worker used by the web app.

    Serves run requests read as JSON lines from stdin (one per run). Each
    request may override form fields via keys matching the FORM_* env
    names. Emits RUN_DONE / READY sentinels on stdout so the caller can
    track lifecycle.

    A "STOP" line aborts the current run (if any) and closes the browser;
    a "QUIT" line shuts the worker down. Both arrive via the stdin reader
    thread, so Stop can interrupt a run that is mid-fill or mid-captcha
    instead of waiting for it to finish.

    With KEEP_BROWSER_OPEN=False Chrome is only alive during a run (it
    opens on demand and closes after), so nothing visible/taskbar lingers
    when idle. The worker itself stays up either way.

    The worker is self-healing: if the browser dies mid-run (e.g. the
    window is closed manually) it restarts Chrome and keeps serving.
    """
    driver = None
    req_queue = queue.Queue()
    stop_event = threading.Event()
    threading.Thread(
        target=_stdin_reader, args=(req_queue, stop_event), daemon=True
    ).start()

    def open_browser():
        d = start_driver()
        if not _alive(d):
            print("[!] Browser died during startup; recreating.")
            d = _recreate_driver(d)
        return d

    def close_browser():
        nonlocal driver
        close_automation_browser(driver)
        driver = None
        print("[service] Browser closed.")

    mode = "HEADLESS" if HEADLESS else "VISIBLE"
    keep = "stays open between runs" if KEEP_BROWSER_OPEN else "opens per run"
    print(f"[service] Worker ready. Browser {keep} ({mode} mode).")
    print("READY", flush=True)
    running = False
    try:
        while True:
            line = req_queue.get()
            if line == "QUIT":
                break
            if line == "STOP":
                if running:
                    print("[!] Stop received; aborting run and closing browser.")
                else:
                    print("[*] Stop received; nothing running.")
                close_browser()
                stop_event.clear()
                continue

            stop_event.clear()
            try:
                req = json.loads(line)
            except Exception:
                req = {}
            for key, value in (req or {}).items():
                os.environ["FORM_" + key.upper()] = str(value)

            # Open the browser for this run (or reload if it was kept open).
            _reset_timings()
            if driver is None:
                driver = open_browser()
                print("[service] Browser opened for run.")
            else:
                try:
                    driver.get(TARGET_URL)
                except Exception as e:
                    print(f"[!] Page load failed: {e}; restarting browser.")
                    driver = _recreate_driver(driver)

            running = True
            try:
                run_once(driver, abort_event=stop_event)
            except Exception as e:
                print(f"[!] Run failed: {e}")
                if any(k in str(e).lower() for k in ("window", "session", "disconnected")):
                    driver = _recreate_driver(driver)
                    print("[*] Browser recreated; ready for next run.")
            finally:
                running = False

            if stop_event.is_set():
                print("[!] Run aborted by Stop request.")
                close_browser()
                stop_event.clear()
                print("RUN_DONE")
                print("READY", flush=True)
                continue

            print("RUN_DONE")
            if not KEEP_BROWSER_OPEN:
                close_browser()
            print("READY", flush=True)
    finally:
        close_browser()


if __name__ == "__main__":
    if "--service" in sys.argv:
        service_loop()
    else:
        main()