def get_live_nifty_spot():
    """
    Upstox LTP / Quote API द्वारे चालू किंवा शेवटचा क्लोजिंग स्पॉट मिळवणे.
    """
    endpoints = [
        # १. थेट LTP API (बाजार बंद असतानाही शेवटचा LTP देतो)
        "https://api.upstox.com/v2/market-quote/ltp?instrument_key=NSE_INDEX|Nifty%2050",
        # २. Full Quote API
        "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty%2050"
    ]
    
    for url in endpoints:
        try:
            res = session.get(url, headers=UPSTOX_HEADERS, timeout=10)
            if res.status_code == 200:
                data = res.json().get('data', {})
                for key, val in data.items():
                    # 'last_price' (LTP API) किंवा 'last_price' (Quotes API)
                    lp = val.get('last_price')
                    if lp and float(lp) > 10000:
                        return float(lp)
        except Exception as e:
            print(f"[-] Spot Fetch Error ({url}): {e}")
            
    print("❌ त्रुटी: लाईव्ह निफ्टी स्पॉट प्राप्त झाला नाही.")
    return None
