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
        f"response_type=code&client_id={API_KEY}&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )

    print("[*] हेडलेस ऑथेंटिकेशन सुरू होत आहे...", flush=True)

    captured_code = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # १. रिडिरॅक्ट होताना येणारी रिक्वेस्ट ट्रॅक करणे
        def on_request(request):
            nonlocal captured_code
            if request.url.startswith(REDIRECT_URI) and "code=" in request.url:
                parsed = urllib.parse.urlparse(request.url)
                captured_code = urllib.parse.parse_qs(parsed.query).get('code', [None])[0]
                print(f"[+] Auth Code पकडला गेला!", flush=True)

        page.on("request", on_request)

        page.goto(auth_dialog_url, wait_until="networkidle", timeout=60000)

        # २. मोबाईल नंबर भरणे
        print("[*] मोबाईल नंबर इनपुट शोधत आहे...", flush=True)
        mobile_selector = '#mobileNum, input[name="mobileNumber"], input[type="tel"], input[type="text"]'
        page.wait_for_selector(mobile_selector, timeout=30000)
        page.locator(mobile_selector).first.fill(MOBILE_NO)
        
        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Get OTP"), button:has-text("Continue"), #getOtp').first.click(timeout=5000)
        except Exception:
            pass

        # ३. OTP भरणे
        totp = pyotp.TOTP(TOTP_KEY)
        otp = totp.now()
        print(f"[*] जनरेट झालेला OTP टाकत आहे: {otp}", flush=True)

        otp_selector = '#otpNum, input[name="otp"], input[type="number"], input[inputmode="numeric"]'
        page.wait_for_selector(otp_selector, timeout=30000)
        page.locator(otp_selector).first.fill(otp)
        
        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Continue"), button:has-text("Verify"), #verifyOtp').first.click(timeout=5000)
        except Exception:
            pass

        # ४. PIN भरणे
        print("[*] ६-अंकी लॉगिन PIN टाकत आहे...", flush=True)
        pin_selector = '#pinCode, input[type="password"], input[name="pin"]'
        page.wait_for_selector(pin_selector, timeout=30000)
        page.locator(pin_selector).first.fill(PIN)

        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Continue"), button:has-text("Login"), #pinContinueBtn').first.click(timeout=5000)
        except Exception:
            pass

        print("[*] Redirect कोड मिळण्याची वाट पाहत आहे...", flush=True)
        # रिडिरॅक्ट रिक्वेस्ट येईपर्यंत ५ ते १० सेकंद वाट पाहणे
        for _ in range(30):
            if captured_code:
                break
            page.wait_for_timeout(500)

        browser.close()
        return captured_code

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
