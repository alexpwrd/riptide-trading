"""Writes live-status.json every 10s for the arena adapter."""
import json, subprocess, time, os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")

def kraken(args):
    r = subprocess.run([KRAKEN_BIN] + args.split(), capture_output=True, text=True, timeout=15)
    return "\n".join(l for l in r.stdout.strip().split("\n") if not l.startswith("Warning:"))

while True:
    try:
        status = json.loads(kraken("paper status -o json"))
        bal = json.loads(kraken("paper balance -o json"))
        held = [a for a, v in bal.get("balances", {}).items() if v.get("total", 0) > 0 and a != "USD"]
        val = status.get("current_value", 0)
        start = status.get("starting_balance", 1000000)
        pnl = status.get("unrealized_pnl", 0)
        live = {
            "starting_balance": start,
            "current_value": val,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": round(pnl / start * 100, 4) if start else 0,
            "drawdown_pct": status.get("unrealized_pnl_pct", 0),
            "total_trades": status.get("total_trades", 0),
            "pairs": "/".join(held) if held else "",
            "cash": bal.get("balances", {}).get("USD", {}).get("available", 0),
        }
        (DATA_DIR / "live-status.json").write_text(json.dumps(live))
    except Exception as e:
        print(f"Error: {e}")
    time.sleep(10)
