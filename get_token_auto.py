import os
import sys
import urllib.parse
import pyotp
import requests

API_KEY = os.environ.get("UPSTOX_API_KEY", "").strip()
API_SECRET = os.environ.get("UPSTOX_API_SECRET", "").strip()
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "").strip()
MOBILE_NO = os.environ.get("UPSTOX_MOBILE", "").strip()
PIN = os.environ.get("UPSTOX_PIN", "").strip()
TOTP_KEY = os.environ.get("UPSTOX_TOTP_KEY", "").strip()

def generate_upstox_token():
    if not all([API_KEY, API_SECRET, REDIRECT_URI, MOBILE_NO, PIN, TOTP_KEY]):
        print("[-] त्रुटी: आवश्यक Secrets उपलब्ध नाहीत.")
        sys.exit(1)

    session = requests.Session()

    # १. चालू OTP जनरेट करणे
    totp = pyotp.TOTP(TOTP_KEY)
    current_otp = totp.now()
    print(f"[*] चालू OTP जनरेट केला: {current_otp}", flush=True)

    # २. Upstox TOTP व्हॅलिडेशन
    otp_url = "https://api.upstox.com/v2/login/v2/totp"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {
        "code": current_otp,
        "mobileNumber": MOBILE_NO
    }

    res_otp = session.post(otp_url, json=payload, headers=headers, timeout=10)
    if res_otp.status_code != 200:
        print(f"[-] TOTP Validation अयशस्वी ({res_otp.status_code}): {res_otp.text}")
        sys.exit(1)

    # ३. ६-अंकी लॉगिन PIN व्हॅलिडेशन
    pin_url = "https://api.upstox.com/v2/login/v2/pin"
    res_pin = session.post(pin_url, json={"pin": PIN}, headers=headers, timeout=10)
    if res_pin.status_code != 200:
        print(f"[-] PIN Validation अयशस्वी ({res_pin.status_code}): {res_pin.text}")
        sys.exit(1)

    # ४. Authorization Code मिळवणे
    auth_dialog_url = "https://api.upstox.com/v2/login/authorization/dialog"
    params = {
        "response_type": "code",
        "client_id": API_KEY,
        "redirect_uri": REDIRECT_URI
    }
    res_auth = session.get(auth_dialog_url, params=params, allow_redirects=False, timeout=10)
    redirect_location = res_auth.headers.get("Location", "")

    parsed = urllib.parse.urlparse(redirect_location)
    auth_code = urllib.parse.parse_qs(parsed.query).get('code', [None])[0]

    if not auth_code:
        print("[-] Auth Code मिळाला नाही.")
        sys.exit(1)

    # ५. Auth Code द्वारे नवीन Access Token मिळवणे
    token_url = "https://api.upstox.com/v2/login/authorization/token"
    token_data = {
        "code": auth_code,
        "client_id": API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    res_token = session.post(token_url, data=token_data, timeout=10)
    if res_token.status_code == 200:
        access_token = res_token.json().get("access_token")
        print("✅ नवीन Upstox Access Token यशस्वीरीत्या मिळाला!", flush=True)
        with open("token.txt", "w") as f:
            f.write(access_token)
    else:
        print(f"[-] Token एक्सचेंज अयशस्वी: {res_token.text}")
        sys.exit(1)

if __name__ == "__main__":
    generate_upstox_token()
