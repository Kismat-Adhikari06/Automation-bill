import random
import re
import subprocess
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================
# CONFIGURATION
# ============================================================
TARGET_URL = "https://prize.ird.gov.np/"

HEADLESS = False  # keep False to preserve real rendering vectors

FAST_MODE = True   # scale all human delays down; False = original speeds
BULK_FILL = True   # fill all form fields in one JS pass; False = one-by-one

CHECKBOX_TIMEOUT = 30  # seconds to wait for iframe / checkbox
SHORT_WAIT = 3         # seconds to wait per checkbox selector before moving on
TOKEN_WAIT = 5         # seconds to let token validation finish
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


def fill_form(driver, data):
    """Fill the prize-check form inputs.

    In BULK_FILL mode every field is set in a single JS pass (native value
    setter + input/change events, so React-style validation still fires).
    Falls back to the one-by-one human-paced fill if the bulk pass fails.
    """
    fields = list(data.items())
    if not BULK_FILL:
        _fill_form_human(driver, fields)
        return

    try:
        names = [n for n, _ in fields]
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
        WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, f"input[name='{names[0]}']"))
        )
        missing = driver.execute_script(script, names, [v for _, v in fields])
        if missing:
            print(f"[!] Bulk fill missed: {missing}; falling back to one-by-one.")
            _fill_form_human(driver, fields)
        else:
            for name, value in fields:
                print(f"[+] Filled {name}: {value}")
    except Exception as e:
        print(f"[!] Bulk fill failed ({e}); falling back to one-by-one.")
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


def click_submit(driver):
    """Wait for the submit button to become enabled, then click it.

    The site keeps the button disabled until the Turnstile token is
    granted, so reaching the click also confirms the captcha passed.
    """
    submit_button = WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
        EC.presence_of_element_located(
            (By.XPATH,
             "//button[normalize-space(.)='Generate prize coupon' "
             "or normalize-space(.)='पुरस्कार कुपन सिर्जना गर्नुहोस्'] | "
             "//button[@type='submit'][contains(@class, 'primary-button')]")
        )
    )
    WebDriverWait(driver, CHECKBOX_TIMEOUT).until(
        lambda d: submit_button.is_enabled()
    )
    print("[*] Submit button enabled (captcha passed).")
    human_delay()

    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", submit_button
    )
    human_delay(0.4, 1.2)

    try:
        submit_button.click()
    except Exception:
        driver.execute_script(
            "arguments[0].click();", submit_button
        )
    print("[+] Submit button clicked.")


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

def main():
    driver = start_driver()
    try:
        human_delay()

        print("[1/3] Filling form...")
        fill_form(driver, DUMMY_FORM_DATA)

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

        # Give the background telemetry / token validation time to pass.
        # click_submit() already waits for the button to unlock, so this is
        # just a short grace period, not the main wait.
        print("[*] Waiting for token validation...")
        time.sleep(TOKEN_WAIT if not FAST_MODE else 2)

        print("[3/3] Submitting...")
        # Automatically submit once the captcha token unlocks the button.
        click_submit(driver)

        # Pause so you can see the result page before the browser closes.
        time.sleep(TOKEN_WAIT if not FAST_MODE else 3)

        input("[*] Press Enter to close the browser...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
