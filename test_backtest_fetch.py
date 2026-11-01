import sys
import json
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone

PROXY = "http://127.0.0.1:7897"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Setup proxy handler
proxy_handler = urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
opener = urllib.request.build_opener(proxy_handler)

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return None

print("Testing Gamma connection...")
# Test fetching 5 recent 5m markets
now_ts = int(time.time())
cur_5m = (now_ts // 300) * 300

markets = []
print(f"Scanning recent markets around {cur_5m}...")
for i in range(1, 30):
    ts = cur_5m - (i * 300)
    slug = f"btc-updown-5m-{ts}"
    event = fetch_json(f"{GAMMA}/events/slug/{slug}")
    if event and event.get("markets"):
        m = event["markets"][0]
        if m.get("closed") and m.get("outcomePrices"):
            markets.append({
                "slug": slug,
                "ts": ts,
                "outcomes": json.loads(m.get("outcomes", '["Up","Down"]')),
                "outcomePrices": json.loads(m.get("outcomePrices", '["0","0"]')),
                "tokenIds": json.loads(m.get("clobTokenIds", '[]')),
                "eventMetadata": event.get("eventMetadata")
            })
            print(f"  Found {slug}: outcomes={markets[-1]['outcomes']}, prices={markets[-1]['outcomePrices']}")
    time.sleep(0.1)

print(f"Total resolved markets retrieved: {len(markets)}")
