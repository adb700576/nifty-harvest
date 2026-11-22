import os
import sys
import time
import pyotp
import logging
import requests
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TOKEN_AUTO] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("TOKEN_AUTO")

API_KEY = os.getenv("UPSTOX_API_KEY", "").strip()
API_SECRET = os.getenv("UPSTOX_API_SECRET", "").strip()
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1:5000/").strip()
MOBILE_NO = (os.getenv("UPSTOX_MOBILE") or os.getenv("UPSTOX_MOBILE_NO") or "").strip()
PIN = os.getenv("UPSTOX_PIN", "").strip()
TOTP_SECRET = (os.getenv("UPSTOX_TOTP_KEY") or os.getenv("UPSTOX_TOTP_SECRET") or "").strip()
TOKEN_FILE = "ACCESS_TOKEN.txt"


def exchange_code_for_token(auth_code: str) -> str:
    url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "code": auth_code,
        "client_id": API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    resp = requests.post(url, headers=headers, data=data, timeout=15).json()
    return str(resp.get("access_token", "")).strip()


def generate_token():
    if not all([API_KEY, API_SECRET, MOBILE_NO, PIN, TOTP_SECRET]):
        log.error("Missing Upstox login credentials in environment variables.")
        sys.exit(1)

    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
    auth_code = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(req):
            nonlocal auth_code
            if req.url.startswith(REDIRECT_URI) and "code=" in req.url:
                qs = parse_qs(urlparse(req.url).query)
                if "code" in qs:
                    auth_code = qs["code"][0]

        page.on("request", handle_request)
        page.goto(auth_url, wait_until="networkidle", timeout=30000)

        page.fill("#mobileNum", MOBILE_NO)
        page.click("#getOtp")
        page.wait_for_selector("#otpNum", timeout=15000)

        otp = pyotp.TOTP(TOTP_SECRET).now()
        page.fill("#otpNum", otp)
        page.click("#continueBtn")

        page.wait_for_selector("#pinCode", timeout=15000)
        page.fill("#pinCode", PIN)
        page.click("#pinContinueBtn")

        for _ in range(30):
            if auth_code:
                break
            time.sleep(0.5)

        browser.close()

    if not auth_code:
        log.error("Failed to capture authorization code.")
        sys.exit(1)

    access_token = exchange_code_for_token(auth_code)
    if not access_token:
        log.error("Failed to exchange auth code for access token.")
        sys.exit(1)

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(access_token)

    log.info("Upstox Access Token generated and saved silently to ACCESS_TOKEN.txt.")


if __name__ == "__main__":
    generate_token()
