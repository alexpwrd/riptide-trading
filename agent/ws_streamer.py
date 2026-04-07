"""
WebSocket Price Streamer
Subscribes to Kraken WebSocket for real-time prices.
Writes prices.json every update — sub-second freshness.
The sentinel reads this instead of polling kraken ticker.
"""

import json
import subprocess
import time
import signal
import sys
import os
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")
PRICES_FILE = DATA_DIR / "ws_prices.json"

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", 
         "AVAX/USD", "LINK/USD", "ALGO/USD", "SUI/USD", "FET/USD"]

running = True
def signal_handler(sig, frame):
    global running
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def stream():
    """Run kraken ws ticker and parse the JSON stream."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pairs_arg = " ".join(PAIRS)
    
    print(f"  WebSocket Streamer starting...")
    print(f"  Pairs: {pairs_arg}")
    print(f"  Writing to: {PRICES_FILE}")
    
    prices = {}
    
    cmd = f"{KRAKEN_BIN} ws ticker {pairs_arg} -o json"
    proc = subprocess.Popen(
        cmd.split(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1
    )
    
    try:
        for line in proc.stdout:
            if not running:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                # Kraken WS v2 ticker format
                ticker_data = data.get("data", [])
                for tick in ticker_data if isinstance(ticker_data, list) else [ticker_data]:
                    symbol = tick.get("symbol", "")
                    last = tick.get("last", 0)
                    if symbol and last:
                        pair_key = symbol.replace("/", "")
                        prices[pair_key] = {
                            "price": float(last),
                            "bid": float(tick.get("bid", 0)),
                            "ask": float(tick.get("ask", 0)),
                            "high_24h": float(tick.get("high", 0)),
                            "low_24h": float(tick.get("low", 0)),
                            "volume": float(tick.get("volume", 0)),
                            "change_pct": float(tick.get("change_pct", 0)),
                            "updated": datetime.now().isoformat()
                        }
                        # Write immediately
                        snapshot = {
                            "timestamp": datetime.now().isoformat(),
                            "source": "websocket",
                            "prices": prices
                        }
                        PRICES_FILE.write_text(json.dumps(snapshot))
            except json.JSONDecodeError:
                continue
            except Exception as e:
                print(f"  WS parse error: {e}")
    finally:
        proc.terminate()
        print("  WebSocket Streamer stopped")

if __name__ == "__main__":
    stream()
