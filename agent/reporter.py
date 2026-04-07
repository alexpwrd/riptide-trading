"""
ClawdTrader Discord Reporter
Read-only analyst — reviews portfolio and market, reports to Discord every 10 min.
Uses phi4-mini (194 tok/s). Never trades.
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
MODEL = "phi4-mini"
INTERVAL = 600  # 10 minutes
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")
DISCORD_WEBHOOK = os.environ.get("CLAWDTRADER_DISCORD_WEBHOOK", "")

running = True
def signal_handler(sig, frame):
    global running
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def kraken_cmd(args):
    try:
        r = subprocess.run([KRAKEN_BIN] + args.split(), capture_output=True, text=True, timeout=30)
        return "\n".join(l for l in r.stdout.strip().split("\n") if not l.startswith("Warning:"))
    except:
        return "{}"

def post_discord_embed(title, fields, color=0x6366f1):
    if not DISCORD_WEBHOOK: return
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"ClawdTrader Analyst | phi4-mini | {datetime.now().strftime('%H:%M UTC')}"},
        "timestamp": datetime.now().isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK, json={"username": "ClawdTrader Analyst", "embeds": [embed]}, timeout=10)
    except: pass

def run():
    print(f"  [REPORTER] phi4-mini, every {INTERVAL//60}min — read-only Discord analyst")
    time.sleep(30)  # Let engine start first
    
    while running:
        start = time.time()
        try:
            # Gather data
            status = json.loads(kraken_cmd("paper status -o json"))
            bal = json.loads(kraken_cmd("paper balance -o json"))
            balances = bal.get("balances", {})
            cash = balances.get("USD", {}).get("available", 0)
            
            # Positions with current prices
            pos_lines = []
            total_pos_pnl = 0
            pos_file = DATA_DIR / "positions.json"
            pos_data = json.loads(pos_file.read_text()) if pos_file.exists() else {}
            
            for asset, info in balances.items():
                if asset == "USD" or asset == "USDT" or info.get("total", 0) == 0: continue
                total = info["total"]
                entry = pos_data.get(asset, {}).get("entry_price", 0)
                try:
                    t = json.loads(kraken_cmd(f"ticker {asset}USD -o json"))
                    for k, v in t.items():
                        price = float(v["c"][0])
                        val = price * total
                        if entry > 0:
                            pnl_pct = (price - entry) / entry * 100
                            total_pos_pnl += val - (entry * total)
                            pos_lines.append(f"{asset}: ${val:,.0f} ({pnl_pct:+.1f}%)")
                        else:
                            pos_lines.append(f"{asset}: ${val:,.0f}")
                except: pass
            
            # Market sentiment from scanner
            market = {}
            mkt_file = DATA_DIR / "market.json"
            if mkt_file.exists():
                try: market = json.loads(mkt_file.read_text())
                except: pass
            
            sentiment = market.get("sentiment", "?")
            positive = market.get("positive", 0)
            total_pairs = market.get("total", 0)
            top_movers = market.get("movers", [])[:5]
            movers_text = " | ".join(f"{m['pair'].replace('USD','')}: {m['change']:+.1f}%" for m in top_movers)
            
            # Build prompt for analysis
            val = status.get("current_value", 0)
            pnl = status.get("unrealized_pnl", 0)
            pnl_pct = status.get("unrealized_pnl_pct", 0)
            trades = status.get("total_trades", 0)
            
            prompt = f"""You are a trading analyst. Review this portfolio and give a brief assessment (3-4 sentences max).

Portfolio: ${val:,.0f} ({pnl_pct:+.2f}% PNL) | {trades} trades | Cash: ${cash:,.0f}
Positions: {', '.join(pos_lines) if pos_lines else 'None'}
Position PNL: ${total_pos_pnl:,.0f}
Market: {sentiment} ({positive}/{total_pairs} green) | Top: {movers_text}

Assess: 1) Are we positioned well? 2) What's the biggest risk? 3) What should we watch for?
Keep it brief and actionable. No disclaimers."""

            # Get analysis
            resp = requests.post(OLLAMA_URL, json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }, timeout=30)
            
            analysis = resp.json().get("message", {}).get("content", "No analysis")
            if "</think>" in analysis:
                analysis = analysis.split("</think>")[-1].strip()
            analysis = analysis[:500]
            
            # Color: green if positive, red if negative
            color = 0x10b981 if pnl >= 0 else 0xff4444
            
            # Post to Discord
            fields = [
                {"name": "Portfolio", "value": f"${val:,.0f} ({pnl_pct:+.2f}%)", "inline": True},
                {"name": "Cash", "value": f"${cash:,.0f}", "inline": True},
                {"name": "Trades", "value": str(trades), "inline": True},
                {"name": "Positions", "value": "\n".join(pos_lines[:8]) if pos_lines else "None", "inline": False},
                {"name": "Market", "value": f"{sentiment.upper()} ({positive}/{total_pairs}) | {movers_text}", "inline": False},
                {"name": "Analysis", "value": analysis, "inline": False},
            ]
            
            post_discord_embed(f"Portfolio Report — {datetime.now().strftime('%H:%M UTC')}", fields, color)
            print(f"  [REPORTER {datetime.now().strftime('%H:%M:%S')}] Posted: {analysis[:80]}")
            
        except Exception as e:
            print(f"  [REPORTER] Error: {e}")
        
        elapsed = time.time() - start
        time.sleep(max(1, INTERVAL - elapsed))

if __name__ == "__main__":
    run()
