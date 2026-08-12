import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

TARGET = "https://prize.ird.gov.np/"

driver = uc.Chrome()
try:
    driver.get(TARGET)
    time.sleep(8)

    widget = driver.find_elements(By.CLASS_NAME, "turnstile-widget")
    print("turnstile-widget count:", len(widget))
    if widget:
        el = widget[0]
        print("outerHTML:", el.get_attribute("outerHTML")[:1200])
        print("has shadowRoot:", driver.execute_script("return !!arguments[0].shadowRoot;", el))
        print("shadow innerHTML:", str(driver.execute_script(
            "return arguments[0].shadowRoot ? arguments[0].shadowRoot.innerHTML : null;", el
        ))[:2500])

    frames = driver.execute_script(
        "return Array.from(document.querySelectorAll('iframe')).map(f=>f.src);"
    )
    print("iframes in top doc:", frames)

    roots = driver.execute_script(
        """
        const out=[];
        const walk=(root)=>{
          for(const el of root.querySelectorAll('*')){
            if(el.shadowRoot){
              out.push(el.tagName+'.'+(el.className||'').toString().slice(0,60));
              walk(el.shadowRoot);
            }
          }
        };
        walk(document);
        return out;
        """
    )
    print("shadow roots found:", roots)

    errs = driver.find_elements(By.CSS_SELECTOR, ".field-error, [role='alert']")
    print("field errors:", [e.text for e in errs])

    print("turnstile global:", driver.execute_script("return typeof window.turnstile;"))

    btns = driver.find_elements(By.XPATH, "//button[contains(@class,'primary-button')]")
    for b in btns:
        print("button:", repr(b.text[:60]), "| disabled:", b.get_attribute("disabled"))

    input("Press Enter to close the browser...")
finally:
    driver.quit()
