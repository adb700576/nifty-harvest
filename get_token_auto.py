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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert fail: {e}", flush=True)


def get_auth_code_via_headless():
    auth_dialog_url = (
        f"https://api.upstox.com/v2/login/authorization/dialog?"
        f"response_type=code&client_id={API_KEY}&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )

    print("[*] Headless authentication suru karat aahe...", flush=True)
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

        def on_request(request):
            nonlocal captured_code
            if request.url.startswith(REDIRECT_URI) and "code=" in request.url:
                parsed = urllib.parse.urlparse(request.url)
                captured_code = urllib.parse.parse_qs(parsed.query).get('code', [None])[0]
                print(f"[+] Auth Code pakadla!", flush=True)

        page.on("request", on_request)
        page.goto(auth_dialog_url, wait_until="networkidle", timeout=60000)

        # 1. Mobile number
        mobile_selector = '#mobileNum, input[name="mobileNumber"], input[type="tel"], input[type="text"]'
        page.wait_for_selector(mobile_selector, timeout=30000)
        page.locator(mobile_selector).first.fill(MOBILE_NO)
        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Get OTP"), button:has-text("Continue"), #getOtp').first.click(timeout=5000)
        except Exception:
            pass

        # 2. OTP Input
        totp = pyotp.TOTP(TOTP_KEY)
        otp = totp.now()
        otp_selector = '#otpNum, input[name="otp"], input[type="number"], input[inputmode="numeric"]'
        page.wait_for_selector(otp_selector, timeout=30000)
        
        # काही वेळेस 6 स्वतंत्र इनपुट बॉक्स असतात तर काही वेळेस 1 बॉक्स
        otp_inputs = page.locator(otp_selector)
        if otp_inputs.count() == 6:
            for idx, digit in enumerate(otp):
                otp_inputs.nth(idx).fill(digit)
        else:
            otp_inputs.first.fill(otp)

        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Continue"), button:has-text("Verify"), #verifyOtp').first.click(timeout=5000)
        except Exception:
            pass

        # 3. 6-digit PIN Input
        pin_selector = '#pinCode, input[type="password"], input[name="pin"]'
        page.wait_for_selector(pin_selector, timeout=30000)
        
        pin_inputs = page.locator(pin_selector)
        if pin_inputs.count() == 6:
            for idx, digit in enumerate(PIN):
                pin_inputs.nth(idx).fill(digit)
        else:
            pin_inputs.first.fill(PIN)

        page.keyboard.press("Enter")
        try:
            page.locator('button:has-text("Continue"), button:has-text("Login"), #pinContinueBtn').first.click(timeout=5000)
        except Exception:
            pass

        # 4. Wait for redirect code
        for _ in range(40):
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
            send_telegram_msg("❌ *Upstox Alert:* Auth Code milala nahi. Login fail zale!")
            sys.exit(1)

        token = fetch_access_token(code)
        if token:
            print("✅ Navin Upstox Access Token milala!", flush=True)

            # दोन्ही फाईल्समध्ये सेव्ह करणे जेणेकरून कोणतीही स्क्रिप्ट फेल होणार नाही
            with open("ACCESS_TOKEN.txt", "w", encoding="utf-8") as f:
                f.write(token.strip())
            with open("token.txt", "w", encoding="utf-8") as f:
                f.write(token.strip())

            # GitHub Actions Environment मध्ये एक्सपोर्ट करणे
            github_env = os.environ.get("GITHUB_ENV")
            if github_env and os.path.exists(github_env):
                with open(github_env, "a", encoding="utf-8") as env_file:
                    env_file.write(f"\nUPSTOX_ACCESS_TOKEN={token.strip()}\n")
                print("[+] Token injected into GITHUB_ENV", flush=True)

            send_telegram_msg("🚀 *Upstox Alert:* Navin Access Token sakali यशस्वीरीत्या generate zala aahe! Bot ready aahe.")
        else:
            send_telegram_msg("❌ *Upstox Alert:* Token Exchange fail zale!")
            sys.exit(1)
    except Exception as e:
        send_telegram_msg(f"❌ *Upstox Error:* `{e}`")
        sys.exit(1)
