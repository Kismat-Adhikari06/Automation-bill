import json
import os
import random
import re
import subprocess
import sys
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================
# CONFIGURATION
# ============================================================
TARGET_URL = "https://prize.ird.gov.np/"

HEADLESS = False  # True = no visible window (faster) but Turnstile fails on this site
HIDE_WINDOW = False  # False = visible Chrome window (headful)
KEEP_BROWSER_OPEN = False  # False = Chrome opens per run & closes after (no lingering taskbar icon)

FAST_MODE = True   # scale all human delays down; False = original speeds
BULK_FILL = True   # fill all form fields in one JS pass; False = one-by-one

CHECKBOX_TIMEOUT = 30  # seconds to wait for iframe / checkbox
SHORT_WAIT = 3         # seconds to wait per checkbox selector before moving on
TOKEN_WAIT = 5         # seconds to let token validation finish
AUTO_PASS_WAIT = 12    # seconds to wait for auto-pass before manual captcha fallback
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
    """
    fields = list(data.items())
    if not BULK_FILL:
        _fill_form_human(driver, fields)
        return

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
        _fill_form_human(driver, fields)
        return

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
            return
        print(f"[!] Fill attempt {attempt} didn't stick ({bad} mismatched); retrying.")
        time.sleep(0.5)

    print("[!] Bulk fill still failing; falling back to one-by-one.")
    _fill_form_human(driver, fields)


def _fill_form_human(driver, fields):
    """Original one-field-at-a-time fill with human-paced delays."""
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
        except Exception:
            print(f"[!] Could not find input field: {field_name}")


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


def click_submit(driver):
    """Wait for the submit button to become enabled, then click it.

    The site keeps the button disabled until the Turnstile token is
    granted. Most runs auto-pass within ~13s; if the button hasn't
    unlocked after AUTO_PASS_WAIT, we fall back to clicking the visible
    Turnstile checkbox manually, then keep waiting for the unlock.
    """
    submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
        EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
    )
    print("[*] Waiting for submit button to unlock...")
    try:
        WebDriverWait(driver, AUTO_PASS_WAIT).until(
            lambda d: submit_button.is_enabled()
        )
    except Exception:
        print("[!] Captcha didn't auto-pass; trying manual fallback click.")
        _manual_captcha_fallback(driver)
        submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
        )
        try:
            WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
                lambda d: submit_button.is_enabled()
            )
        except Exception:
            # Still locked - try the manual click once more before giving up.
            print("[!] Still locked; retrying captcha click.")
            _manual_captcha_fallback(driver)
            submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
                EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
            )

    WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
        lambda d: submit_button.is_enabled()
    )
    print("[*] Submit button enabled (captcha passed).")

    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", submit_button
    )
    human_delay(0.4, 1.2)

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


def chrome_version_main():
    """Return the installed Chrome major version (e.g. 151) or None.

    Pinning version_main to the real installed Chrome prevents uc from
    launching with a stale/mismatched chromedriver, which on Windows shows
    up as "no such window: target window already closed" right after start.
    """
    for key in [
        r"HKCU\Software\Google\Chrome\BLBeacon",
        r"HKLM\SOFTWARE\Google\Chrome\BLBeacon",
    ]:
        try:
            out = subprocess.run(
                ["reg", "query", key, "/v", "version"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            m = re.search(r"\b(\d+)\.", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


def create_driver():
    """Build the undetected Chrome driver with stability-focused flags."""
    options = uc.ChromeOptions()
    if HEADLESS:
        # Modern headless mode that preserves the real rendering path.
        options.add_argument("--headless=new")

    # Don't wait for all background scripts (Cloudflare/Turnstile) to finish
    # loading before starting; our WebDriverWait calls handle readiness.
    options.page_load_strategy = "eager"

    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-infobars")
    if HIDE_WINDOW:
        # Keep a real, rendering browser (the captcha needs it) but position
        # the window off-screen so the user never sees it. Off-screen does
        # NOT throttle timers the way minimize/hidden tabs do.
        options.add_argument("--window-position=-32000,-32000")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    kwargs = {"options": options}
    version = chrome_version_main()
    if version is not None:
        kwargs["version_main"] = version
    return uc.Chrome(**kwargs)


def start_driver():
    """Create a driver and load the page, retrying up to 3 times.

    Chrome sometimes opens and instantly closes on the first try
    (locked user-data-dir, stale driver cache, etc.). A fresh attempt
    usually fixes it without user intervention.
    """
    last_err = None
    for attempt in range(1, 4):
        driver = None
        try:
            driver = create_driver()
            driver.get(TARGET_URL)
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

def run_once(driver):
    """Execute one full fill -> captcha -> submit cycle on an open driver."""
    human_delay()

    print("[1/3] Filling form...")
    fill_form(driver, build_form_data())

    print("[2/3] Solving captcha...")
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

    print("[3/3] Submitting...")
    # Automatically submit once the captcha token unlocks the button.
    # click_submit() handles all waiting for the token/button.
    clicked = click_submit(driver)

    # Pause briefly for the result page to render.
    time.sleep(TOKEN_WAIT if not FAST_MODE else 3)

    if clicked:
        print("=" * 50)
        print("[+] RESULT: BUTTON WAS CLICKED SUCCESSFULLY.")
        print("=" * 50)
    return clicked


def main():
    driver = start_driver()
    try:
        run_once(driver)
        if HEADLESS or os.environ.get("WEB_MODE"):
            # Nothing to look at (headless) or the web app owns the console.
            print("[*] Run finished; closing browser.")
        else:
            input("[*] Press Enter to close the browser...")
    finally:
        driver.quit()


def _recreate_driver(driver):
    """Safely tear down a dead driver and start a fresh one."""
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass
    return start_driver()


def _alive(driver):
    """True if the driver still has a live browser session."""
    try:
        driver.current_url
        return True
    except Exception:
        return False


def service_loop():
    """Persistent worker used by the web app.

    Serves run requests read as JSON lines from stdin (one per run). Each
    request may override form fields via keys matching the FORM_* env
    names. Emits RUN_DONE / READY sentinels on stdout so the caller can
    track lifecycle.

    With KEEP_BROWSER_OPEN=False Chrome is only alive during a run (it
    opens on demand and closes after), so nothing visible/taskbar lingers
    when idle. The worker itself stays up either way.

    The worker is self-healing: if the browser dies mid-run (e.g. the
    window is closed manually) it restarts Chrome and keeps serving.
    """
    driver = None

    def open_browser():
        d = start_driver()
        if not _alive(d):
            print("[!] Browser died during startup; recreating.")
            d = _recreate_driver(d)
        return d

    print("[service] Worker ready. Chrome opens per run.")
    print("READY", flush=True)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line == "QUIT":
                break
            try:
                req = json.loads(line)
            except Exception:
                req = {}
            for key, value in (req or {}).items():
                os.environ["FORM_" + key.upper()] = str(value)

            # Open the browser for this run (or reload if it was kept open).
            if driver is None:
                driver = open_browser()
                print("[service] Browser opened for run.")
            else:
                try:
                    driver.get(TARGET_URL)
                except Exception as e:
                    print(f"[!] Page load failed: {e}; restarting browser.")
                    driver = _recreate_driver(driver)

            try:
                run_once(driver)
            except Exception as e:
                print(f"[!] Run failed: {e}")
                if any(k in str(e).lower() for k in ("window", "session", "disconnected")):
                    driver = _recreate_driver(driver)
                    print("[*] Browser recreated; ready for next run.")

            print("RUN_DONE")
            if not KEEP_BROWSER_OPEN:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
                print("[service] Browser closed.")
            print("READY", flush=True)
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    if "--service" in sys.argv:
        service_loop()
    else:
        main()
