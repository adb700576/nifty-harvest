import os
import sys
import urllib.parse
import pyotp
import requests
from playwright.sync_api import sync_playwright

API_KEY = os.environ.get("UPSTOX_API_KEY", "").strip()
API_SECRET = os.environ.get("UPSTOX_API_SECRET", "").strip()
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "").strip()
MOBILE_NO = os.environ.get("UPSTOX_MOBILE", "").strip()
PIN = os.environ.get("UPSTOX_PIN", "").strip()
TOTP_KEY = os.environ.get("UPSTOX_TOTP_KEY", "").strip()

def get_auth_code_via_headless():
    auth_dialog_url = (
        f"https://api.upstox.com/v2/login/authorization/dialog?"
        f"response_type=code&client_id={API_KEY}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    )

    print("[*] हेडलेस ऑथेंटिकेशन सुरू होत आहे...", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(auth_dialog_url, timeout=60000)

        # १. मोबाईल नंबर टाकणे
        print("[*] मोबाईल नंबर टाकत आहे...", flush=True)
        page.wait_for_selector('input[type="tel"]', timeout=30000)
        page.fill('input[type="tel"]', MOBILE_NO)
        page.click('button:has-text("Get OTP")')

        # २. TOTP जनरेट करून भरणे
        totp = pyotp.TOTP(TOTP_KEY)
        otp = totp.now()
        print(f"[*] जनरेट झालेला OTP टाकत आहे: {otp}", flush=True)

        page.wait_for_selector('input[type="number"], input[inputmode="numeric"]', timeout=30000)
        page.fill('input[type="number"], input[inputmode="numeric"]', otp)
        page.click('button:has-text("Continue")')

        # ३. ६-अंकी PIN भरणे
        print("[*] ६-अंकी लॉगिन PIN टाकत आहे...", flush=True)
        page.wait_for_selector('input[type="password"]', timeout=30000)
        page.fill('input[type="password"]', PIN)
        page.click('button:has-text("Continue")')

        # ४. रिडिरॅक्ट URL मधून कोड मिळवणे
        print("[*] रिडिरॅक्शन कोडची वाट पाहत आहे...", flush=True)
        page.wait_for_url(f"{REDIRECT_URI}*", timeout=40000)
        final_url = page.url
        browser.close()

        parsed = urllib.parse.urlparse(final_url)
        code = urllib.parse.parse_qs(parsed.query).get('code', [None])[0]
        return code

def fetch_access_token(auth_code):
    token_url = "https://api.upstox.com/v2/login/authorization/token"
    token_data = {
        "code": auth_code,
        "client_id": API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    res = requests.post(token_url, data=token_data, headers=headers, timeout=10)
    if res.status_code == 200:
        return res.json().get("access_token")
    else:
        print(f"[-] Token Exchange Failed: {res.text}", flush=True)
        return None

if __name__ == "__main__":
    try:
        code = get_auth_code_via_headless()
        if not code:
            print("[-] Auth code मिळाला नाही.", flush=True)
            sys.exit(1)

        token = fetch_access_token(code)
        if token:
            print("✅ नवीन Upstox Access Token यशस्वीरीत्या मिळाला!", flush=True)
            with open("token.txt", "w") as f:
                f.write(token)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"[-] अपवाद आला: {e}", flush=True)
        sys.exit(1)
