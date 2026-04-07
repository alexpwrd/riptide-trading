"""
ClawdTrader Sentinel — Fast position monitor
Runs every 30s using qwen3.5:9b for instant decisions.
Checks stops and take-profits, executes immediately.
Does NOT do deep analysis — that's the main agent's job.
"""

import json
import subprocess
import time
import signal
import sys
import os
import requests
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
OLLAMA_URL = "http://localhost:11434/api/chat"
SENTINEL_MODEL = os.environ.get("SENTINEL_MODEL", "qwen3.5:9b")
SENTINEL_INTERVAL = int(os.environ.get("SENTINEL_INTERVAL", "30"))
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")

running = True
def signal_handler(sig, frame):
    global running
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def kraken_cmd(args):
    try:
        r = subprocess.run([KRAKEN_BIN] + args.split(), capture_output=True, text=True, timeout=15)
        out = "\n".join(l for l in r.stdout.strip().split("\n") if not l.startswith("Warning:"))
        return out or r.stderr.strip()
    except Exception as e:
        return json.dumps({"error": str(e)})

def get_positions_with_prices():
    """Get current positions with live prices and P&L."""
    bal = json.loads(kraken_cmd("paper balance -o json"))
    balances = bal.get("balances", {})
    cash = balances.get("USD", {}).get("available", 0)

    # Get trade history for cost basis
    hist = json.loads(kraken_cmd("paper history -o json"))
    trades = hist.get("trades", [])
    costs = {}
    for t in trades:
        asset = t["pair"].replace("USD", "")
        side = t["side"]
        cost = float(t["cost"])
        if asset not in costs:
            costs[asset] = {"buy": 0, "sell": 0}
        costs[asset][side] += cost

    positions = []
    for asset, info in balances.items():
        if asset == "USD" or info.get("total", 0) == 0:
            continue
        total = info["total"]
        net_cost = costs.get(asset, {}).get("buy", 0) - costs.get(asset, {}).get("sell", 0)
        try:
            pair = asset + "USD"
            ticker = json.loads(kraken_cmd(f"ticker {pair} -o json"))
            for k, v in ticker.items():
                price = float(v["c"][0])
                mkt_val = price * total
                pnl = mkt_val - net_cost
                pnl_pct = (pnl / net_cost * 100) if net_cost > 0 else 0
                positions.append({
                    "asset": asset, "pair": pair, "amount": total,
                    "cost": net_cost, "value": mkt_val, "price": price,
                    "pnl": pnl, "pnl_pct": pnl_pct
                })
        except Exception:
            pass

    return positions, cash

def quick_llm_decision(prompt):
    """Fast LLM call with small model — expects simple JSON response."""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": SENTINEL_MODEL,
            "messages": [
                {"role": "system", "content": "You are a fast trading sentinel. Respond ONLY with a JSON object. No thinking, no explanation. Just the action."},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
        }, timeout=30)
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        # Strip think tags if present
        if "</think>" in content:
            content = content.split("</think>")[-1].strip()
        # Extract JSON
        if "{" in content:
            json_str = content[content.index("{"):content.rindex("}")+1]
            return json.loads(json_str)
    except Exception as e:
        print(f"  LLM error: {e}")
    return {"action": "hold"}

def execute_sell(pair, amount):
    """Execute a market sell."""
    result = kraken_cmd(f"paper sell {pair} {amount} --type market -o json")
    return result

def log_sentinel(data):
    """Log sentinel actions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logfile = DATA_DIR / "sentinel.jsonl"
    entry = {"timestamp": datetime.now().isoformat(), **data}
    with open(logfile, "a") as f:
        f.write(json.dumps(entry) + "\n")

def run():
    print(f"""
  Sentinel Active — {SENTINEL_MODEL}
  Interval: {SENTINEL_INTERVAL}s
  Checking stops and take-profits
""")

    # Load strategy for thresholds
    strategy_file = DATA_DIR / "strategy.json"

    while running:
        start = time.time()
        try:
            # Load current thresholds from strategy
            strategy = {}
            if strategy_file.exists():
                strategy = json.loads(strategy_file.read_text())
            exit_notes = strategy.get("exit_notes", "Cut at -1.5%, profit at +3%")

            positions, cash = get_positions_with_prices()

            if not positions:
                time.sleep(SENTINEL_INTERVAL)
                continue

            # Build position summary for LLM
            pos_lines = []
            alerts = []
            for p in positions:
                pos_lines.append(f"{p['asset']}: {p['amount']:,.0f} units, cost=${p['cost']:,.0f}, value=${p['value']:,.0f}, PNL=${p['pnl']:,.0f} ({p['pnl_pct']:+.2f}%)")
                if p["pnl_pct"] <= -1.5:
                    alerts.append(f"STOP-LOSS ALERT: {p['asset']} is at {p['pnl_pct']:.2f}% — BELOW -1.5% cut threshold!")
                if p["pnl_pct"] >= 3.0:
                    alerts.append(f"TAKE-PROFIT ALERT: {p['asset']} is at {p['pnl_pct']:.2f}% — ABOVE +3% profit threshold!")

            ts = datetime.now().strftime("%H:%M:%S")
            status_line = f"[{ts}] Cash: ${cash:,.0f} | " + " | ".join(f"{p['asset']}:{p['pnl_pct']:+.1f}%" for p in positions)
            print(f"  {status_line}")

            if not alerts:
                log_sentinel({"action": "hold", "positions": len(positions), "status": status_line})
                dur = time.time() - start
                wait = max(1, SENTINEL_INTERVAL - dur)
                time.sleep(wait)
                continue

            # We have alerts — ask the small LLM what to do
            print(f"  {'!'*40}")
            for a in alerts:
                print(f"  {a}")

            prompt = f"""POSITIONS:
{chr(10).join(pos_lines)}

ALERTS:
{chr(10).join(alerts)}

RULES: {exit_notes}
Cash available: ${cash:,.2f}

For each alert, decide: sell (how much) or hold.
Respond with JSON: {{"actions": [{{"asset": "X", "action": "sell", "amount": N, "reason": "..."}}]}}
If no action needed, respond: {{"actions": []}}"""

            decision = quick_llm_decision(prompt)
            actions = decision.get("actions", [])

            for action in actions:
                asset = action.get("asset", "")
                act = action.get("action", "hold")
                amount = action.get("amount", 0)
                reason = action.get("reason", "")

                if act == "sell" and amount > 0:
                    pair = asset + "USD"
                    print(f"  EXECUTING: SELL {amount} {pair} — {reason}")
                    result = execute_sell(pair, amount)
                    print(f"  RESULT: {result[:200]}")
                    log_sentinel({"action": "sell", "asset": asset, "amount": amount, "reason": reason, "result": result[:200]})
                else:
                    print(f"  HOLD {asset}: {reason}")
                    log_sentinel({"action": "hold", "asset": asset, "reason": reason})

        except Exception as e:
            print(f"  Sentinel error: {e}")

        dur = time.time() - start
        wait = max(1, SENTINEL_INTERVAL - dur)
        time.sleep(wait)

if __name__ == "__main__":
    run()
