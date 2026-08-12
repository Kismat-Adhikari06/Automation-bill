# Automation-bill

Selenium automation for the Nepal tax prize-coupon form (prize.ird.gov.np) using undetected-chromedriver, including Cloudflare Turnstile handling.

## Files

- `auto_challenge.py` - main flow: fills the form, handles the Turnstile widget (auto-pass or one-click checkbox), and submits.
- `debug_dom.py` - diagnostic script that dumps the DOM state after page load.
