"""
ClawdTrader v3 — Lean Engine
Two components: Sentinel (pure math, 15s) + Trader (nemotron, 60s)
No redundancy. No conflicts. Fast.
"""

import json
import subprocess
import time
import signal
import sys
import os
import threading
import requests
from datetime import datetime
from pathlib import Path

# ─── ERC-8004 On-Chain Integration ────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from erc8004 import submit_trade_intent, post_checkpoint
    ERC8004_ENABLED = True
    print("  [ERC-8004] On-chain integration loaded (Riptide agentId=24)")
except Exception as e:
    ERC8004_ENABLED = False
    print(f"  [ERC-8004] Not available: {e}")

def erc8004_log_trade(pair, action, amount_usd):
    """Submit trade intent to RiskRouter on Sepolia. Fire-and-forget."""
    if not ERC8004_ENABLED:
        return
    try:
        # RiskRouter caps at $500/trade, scale down
        scaled_amount = min(amount_usd, 500)
        submit_trade_intent(pair, action, scaled_amount)
    except Exception as e:
        print(f"  [ERC-8004] Trade intent failed (non-fatal): {e}")

def erc8004_log_checkpoint(summary, action="HOLD", pair="", amount_usd=0, price_usd=0, score=75):
    """Post checkpoint to ValidationRegistry on Sepolia. Fire-and-forget."""
    if not ERC8004_ENABLED:
        return
    try:
        post_checkpoint(summary, action=action, pair=pair, amount_usd=amount_usd, price_usd=price_usd, score=score)
    except Exception as e:
        print(f"  [ERC-8004] Checkpoint failed (non-fatal): {e}")

DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
OLLAMA_URL = "http://localhost:11434/api/chat"
TRADER_MODEL = "nemotron-cascade-2"
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")
DISCORD_WEBHOOK = os.environ.get("CLAWDTRADER_DISCORD_WEBHOOK", "")
PRISM_API_KEY = os.environ.get("PRISM_API_KEY", "")

SCAN_PAIRS = ["BTCUSD","ETHUSD","SOLUSD","AVAXUSD","ALGOUSD","LINKUSD",
              "XRPUSD","ADAUSD","FETUSD","SUIUSD","WIFUSD","RENDERUSD",
              "JUPUSD","PEPEUSD","BONKUSD","ONDOUSD","TRUMPUSD","KASUSD","TAOUSD","ARBUSD"]

running = True
def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─── Shared utilities ─────────────────────────────────────────
def kraken_cmd(args):
    try:
        r = subprocess.run([KRAKEN_BIN] + args.split(), capture_output=True, text=True, timeout=30)
        out = "\n".join(l for l in r.stdout.strip().split("\n") if not l.startswith("Warning:"))
        return out or r.stderr.strip()
    except subprocess.TimeoutExpired:
        return '{"error": "timeout"}'
    except Exception as e:
        return json.dumps({"error": str(e)})

def post_discord(content):
    if not DISCORD_WEBHOOK: return
    try:
        requests.post(DISCORD_WEBHOOK, json={"username": "ClawdTrader v3", "content": content[:2000]}, timeout=10)
    except: pass

def load_strategy():
    sf = DATA_DIR / "strategy.json"
    if sf.exists():
        try: return json.loads(sf.read_text())
        except: pass
    return {}

def log_action(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now().isoformat(), **data}
    with open(DATA_DIR / "engine.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")

# ─── Market Scanner (pure code, runs inside sentinel) ─────────
def scan_market():
    """Scan all pairs, return sorted by momentum. No LLM needed."""
    movers = []
    for pair in SCAN_PAIRS:
        try:
            raw = kraken_cmd(f"ticker {pair} -o json")
            data = json.loads(raw)
            for k, v in data.items():
                price = float(v["c"][0])
                opn = float(v["o"])
                chg = (price - opn) / opn * 100 if opn > 0 else 0
                high = float(v["h"][1])
                low = float(v["l"][1])
                rng = (high - low) / low * 100 if low > 0 else 0
                rng_pos = (price - low) / (high - low) * 100 if high > low else 50
                movers.append({
                    "pair": pair, "price": price, "change": round(chg, 2),
                    "range": round(rng, 2), "range_pos": round(rng_pos, 0),
                    "volume": float(v["v"][1])
                })
        except: pass
    movers.sort(key=lambda x: abs(x["change"]), reverse=True)
    return movers

# ─── Sentinel (pure math, 15s) ────────────────────────────────
def sentinel_loop():
    interval = 15
    print(f"  [SENTINEL] Pure math, every {interval}s")
    
    while running:
        start = time.time()
        try:
            # Get positions
            bal = json.loads(kraken_cmd("paper balance -o json"))
            balances = bal.get("balances", {})
            cash = balances.get("USD", {}).get("available", 0)
            
            status_data = json.loads(kraken_cmd("paper status -o json"))
            
            # Check each position
            positions = []
            for asset, info in balances.items():
                if asset == "USD" or info.get("total", 0) == 0: continue
                total = info["total"]
                try:
                    t = json.loads(kraken_cmd(f"ticker {asset}USD -o json"))
                    for k, v in t.items():
                        price = float(v["c"][0])
                        positions.append({"asset": asset, "amount": total, "price": price, "value": price * total})
                except: pass
            
            # Read cost basis from positions file
            pos_file = DATA_DIR / "positions.json"
            pos_data = {}
            if pos_file.exists():
                try: pos_data = json.loads(pos_file.read_text())
                except: pass
            
            ts = datetime.now().strftime("%H:%M:%S")
            if positions:
                status_parts = []
                for p in positions:
                    entry = pos_data.get(p["asset"], {}).get("entry_price", 0)
                    if entry > 0:
                        pnl_pct = (p["price"] - entry) / entry * 100
                        status_parts.append(f"{p['asset']}:{pnl_pct:+.1f}%")
                        
                        # STOP-LOSS at -2.5%
                        if pnl_pct <= -2.5:
                            result = kraken_cmd(f"paper sell {p['asset']}USD {p['amount']} --type market -o json")
                            print(f"  [SENTINEL {ts}] STOP {p['asset']} at {pnl_pct:.1f}%")
                            post_discord(f"[STOP] Sold all {p['amount']:,.0f} {p['asset']} at {pnl_pct:.1f}%")
                            log_action({"agent": "sentinel", "action": "stop", "asset": p["asset"], "pnl_pct": pnl_pct})
                            # Remove from positions
                            pos_data.pop(p["asset"], None)
                            pos_file.write_text(json.dumps(pos_data, indent=2))
                        
                        # TAKE-PROFIT: sell half at +3%
                        elif pnl_pct >= 3.0:
                            sell_amt = p["amount"] * 0.5
                            result = kraken_cmd(f"paper sell {p['asset']}USD {sell_amt} --type market -o json")
                            print(f"  [SENTINEL {ts}] PROFIT {p['asset']} at {pnl_pct:.1f}% — sold half")
                            post_discord(f"[PROFIT] Sold half {sell_amt:,.0f} {p['asset']} at +{pnl_pct:.1f}%")
                            log_action({"agent": "sentinel", "action": "take_profit", "asset": p["asset"], "pnl_pct": pnl_pct})
                    else:
                        status_parts.append(f"{p['asset']}")
                
                print(f"  [SENTINEL {ts}] ${cash:,.0f} | {' | '.join(status_parts)}")
            else:
                print(f"  [SENTINEL {ts}] ${status_data.get('current_value',0):,.0f} | ALL CASH")
            
            # Write live status for arena
            try:
                held = [p["asset"] for p in positions]
                live = {
                    "starting_balance": status_data.get("starting_balance", 10000000),
                    "current_value": status_data.get("current_value", 0),
                    "unrealized_pnl": status_data.get("unrealized_pnl", 0),
                    "unrealized_pnl_pct": round(status_data.get("unrealized_pnl", 0) / status_data.get("starting_balance", 10000000) * 100, 4),
                    "total_trades": status_data.get("total_trades", 0),
                    "pairs": "/".join(held),
                    "cash": cash,
                }
                (DATA_DIR / "live-status.json").write_text(json.dumps(live))
            except: pass
            
            # Scan market every other cycle (30s)
            if int(time.time()) % 30 < interval:
                movers = scan_market()
                positive = sum(1 for m in movers if m["change"] > 0)
                sentiment = "bullish" if positive > len(movers) * 0.6 else "bearish" if positive < len(movers) * 0.4 else "neutral"
                snapshot = {
                    "timestamp": datetime.now().isoformat(),
                    "movers": movers[:10],
                    "positive": positive,
                    "total": len(movers),
                    "sentiment": sentiment
                }
                (DATA_DIR / "market.json").write_text(json.dumps(snapshot, indent=1))
        
        except Exception as e:
            print(f"  [SENTINEL] Error: {e}")
        
        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))

# ─── Trader (nemotron, 60s) ───────────────────────────────────
def trader_loop():
    interval = 60
    print(f"  [TRADER] {TRADER_MODEL}, every {interval}s")
    time.sleep(10)
    
    TOOLS = [
        {"type": "function", "function": {"name": "buy", "description": "Buy an asset. Paper trading, zero fees.", "parameters": {"type": "object", "properties": {"pair": {"type": "string"}, "amount": {"type": "number"}, "order_type": {"type": "string", "enum": ["market", "limit"]}, "price": {"type": "number"}}, "required": ["pair", "amount"]}}},
        {"type": "function", "function": {"name": "sell", "description": "Sell an asset you hold.", "parameters": {"type": "object", "properties": {"pair": {"type": "string"}, "amount": {"type": "number"}}, "required": ["pair", "amount"]}}},
        {"type": "function", "function": {"name": "get_ohlc", "description": "Get 1h OHLC candles for trend analysis.", "parameters": {"type": "object", "properties": {"pair": {"type": "string"}}, "required": ["pair"]}}},
        {"type": "function", "function": {"name": "get_orderbook", "description": "Get L2 orderbook for support/resistance.", "parameters": {"type": "object", "properties": {"pair": {"type": "string"}}, "required": ["pair"]}}},
        {"type": "function", "function": {"name": "done", "description": "Finish this cycle.", "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    ]
    
    def execute_tool(name, args):
        if name == "buy":
            pair = args.get("pair", "")
            amount = args.get("amount", 0)
            otype = args.get("order_type", "market")
            price = args.get("price")
            # Cash floor: max buy = 20% of portfolio per trade
            try:
                st_check = json.loads(kraken_cmd("paper status -o json"))
                total_val = st_check.get("current_value", 10000000)
                max_buy = total_val * 0.20  # 20% max per trade
                est_cost = amount * (price if price else 100)  # rough estimate
                if est_cost > max_buy:
                    return json.dumps({"error": f"Trade too large: ${est_cost:,.0f} exceeds 20% limit (${max_buy:,.0f}). Reduce size."})
            except: pass
            # Record entry price
            try:
                t = json.loads(kraken_cmd(f"ticker {pair} -o json"))
                for k, v in t.items():
                    entry_price = float(v["c"][0])
                    asset = pair.replace("USD", "")
                    pos_file = DATA_DIR / "positions.json"
                    pos_data = {}
                    if pos_file.exists():
                        try: pos_data = json.loads(pos_file.read_text())
                        except: pass
                    pos_data[asset] = {"entry_price": entry_price, "time": datetime.now().isoformat()}
                    pos_file.write_text(json.dumps(pos_data, indent=2))
            except: pass
            
            cmd = f"paper buy {pair} {amount} --type {otype} -o json"
            if otype == "limit" and price:
                cmd = f"paper buy {pair} {amount} --type limit --price {price} -o json"
            return kraken_cmd(cmd)
        elif name == "sell":
            return kraken_cmd(f"paper sell {args.get('pair','')} {args.get('amount',0)} --type market -o json")
        elif name == "get_ohlc":
            raw = kraken_cmd(f"ohlc {args.get('pair','')} --interval 60 -o json")
            try:
                data = json.loads(raw)
                for key, candles in data.items():
                    if key == "last": continue
                    recent = candles[-10:]
                    return json.dumps([{"o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[6]} for c in recent])
            except: return raw
        elif name == "get_orderbook":
            raw = kraken_cmd(f"orderbook {args.get('pair','')} -o json")
            try:
                data = json.loads(raw)
                for key, book in data.items():
                    return json.dumps({"asks": [{"p": a[0], "v": a[1]} for a in book.get("asks",[])[:5]], "bids": [{"p": b[0], "v": b[1]} for b in book.get("bids",[])[:5]]})
            except: return raw
        elif name == "done":
            return json.dumps({"status": "done"})
        return json.dumps({"error": f"unknown tool: {name}"})
    
    while running:
        start = time.time()
        try:
            # Auto-cleanup stale orders to prevent order hoarding
            try:
                orders_raw = kraken_cmd("paper orders -o json")
                orders_data = json.loads(orders_raw)
                open_orders = orders_data.get("open_orders", []) if isinstance(orders_data, dict) else orders_data if isinstance(orders_data, list) else []
                order_count = len(open_orders)
                if order_count > 15:
                    # Cancel orders older than 2 hours
                    from datetime import timezone
                    now = datetime.now(timezone.utc)
                    stale = []
                    for o in open_orders:
                        t = o.get("created_at", "")
                        if t:
                            try:
                                created = datetime.fromisoformat(t)
                                age_hours = (now - created).total_seconds() / 3600
                                if age_hours > 2:
                                    stale.append(o.get("id", ""))
                            except: pass
                    if stale:
                        # Cancel stale orders individually
                        cancelled = 0
                        for oid in stale[:50]:  # max 50 per cycle
                            result = kraken_cmd(f"paper cancel {oid} -o json")
                            if "cancel" in result.lower():
                                cancelled += 1
                        if cancelled > 0:
                            ts_now = datetime.now().strftime("%H:%M:%S")
                            print(f"  [TRADER {ts_now}] Cleaned up {cancelled} stale orders (>{2}h old, {order_count} total)")
                    elif order_count > 20:
                        # Too many recent orders — cancel oldest batch
                        oldest = sorted(open_orders, key=lambda o: o.get("created_at", ""))[:order_count - 15]
                        for o in oldest:
                            kraken_cmd(f"paper cancel {o.get('id', '')} -o json")
                        ts_now = datetime.now().strftime("%H:%M:%S")
                        print(f"  [TRADER {ts_now}] Trimmed {len(oldest)} excess orders (keeping 15)")
            except Exception as e:
                pass  # Never crash the engine over cleanup

            # Read pre-computed market data
            market = {}
            mkt_file = DATA_DIR / "market.json"
            if mkt_file.exists():
                try: market = json.loads(mkt_file.read_text())
                except: pass

            # Read portfolio
            status_data = json.loads(kraken_cmd("paper status -o json"))
            bal = json.loads(kraken_cmd("paper balance -o json"))
            balances = bal.get("balances", {})
            cash = balances.get("USD", {}).get("available", 0)
            
            holdings = []
            pos_data = {}
            pos_file = DATA_DIR / "positions.json"
            if pos_file.exists():
                try: pos_data = json.loads(pos_file.read_text())
                except: pass
            
            for asset, info in balances.items():
                if asset == "USD" or info.get("total", 0) == 0: continue
                entry = pos_data.get(asset, {}).get("entry_price", 0)
                holdings.append(f"  {asset}: {info['total']:,.0f} (entry: ${entry:,.4f})" if entry else f"  {asset}: {info['total']:,.0f}")
            
            strategy = load_strategy()
            movers = market.get("movers", [])[:7]
            sentiment = market.get("sentiment", "?")
            movers_text = "\n".join(f"  {m['pair']}: {m['change']:+.1f}% range:{m['range']:.1f}% pos:{m['range_pos']:.0f}%" for m in movers)
            
            prompt = f"""Portfolio: ${status_data.get('current_value',0):,.0f} ({status_data.get('unrealized_pnl_pct',0):+.2f}%)
Cash: ${cash:,.0f}
Holdings:
{chr(10).join(holdings) if holdings else '  None'}

Market ({sentiment}, {market.get('positive',0)}/{market.get('total',0)} green):
{movers_text}

Strategy: {strategy.get('entry_notes', 'Trade with conviction.')}

Decide: buy, sell, or hold. Use get_ohlc or get_orderbook if you need more data on a specific pair. Be decisive. Call done when finished."""

            system = f"""You are a fast crypto trader with $10M. IMPORTANT: Use USD pairs (BTCUSD, ETHUSD, SOLUSD), NOT USDT pairs. Zero fees. Sentinel handles stops at -2.5% and take-profit at +3%.
Your job: find the best opportunity RIGHT NOW and trade it. One position at a time is fine. Don't overthink — act.
Rules: {strategy.get('exit_notes', 'Stop at -2.5%. Take profit at +3%.')}
Max position: {strategy.get('max_position_pct', 40)}% of portfolio."""

            messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"  [TRADER {ts}] Thinking...")
            
            trades_made = []
            for turn in range(15):  # max 15 tool calls
                try:
                    resp = requests.post(OLLAMA_URL, json={
                        "model": TRADER_MODEL, "messages": messages, "tools": TOOLS, "stream": False
                    }, timeout=30)
                    msg = resp.json().get("message", {})
                except Exception as e:
                    print(f"  [TRADER] LLM error: {e}")
                    break
                
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])
                
                if content:
                    display = content
                    if "</think>" in display:
                        display = display.split("</think>")[-1].strip()
                    if display:
                        print(f"  [TRADER] {display[:150]}")
                
                if not tool_calls:
                    break
                
                messages.append(msg)
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except: args = {}
                    
                    if name == "done":
                        summary = args.get("summary", "")
                        print(f"  [TRADER {ts}] Done: {summary[:120]}")
                        # ERC-8004: submit ONE trade intent + ONE checkpoint sequentially (no threading)
                        cycle_action = "HOLD"
                        cycle_pair = ""
                        cycle_amount = 0
                        cycle_price = 0
                        # Find last successful trade for on-chain submission
                        last_success = None
                        if trades_made:
                            for t in reversed(trades_made):
                                if "error" not in t.get("result", "").lower()[:50]:
                                    last_success = t
                                    break
                            last_trade = trades_made[-1]
                            cycle_action = "BUY" if last_trade["tool"] == "buy" else "SELL"
                            cycle_pair = last_trade["args"].get("pair", "")
                            cycle_amount = last_trade["args"].get("amount", 0)
                            cycle_price = last_trade["args"].get("price", 0)
                        # Submit trade intent for last successful trade (only on actual trades, not holds)
                        if last_success:
                            tp = last_success["args"].get("pair", "")
                            ta = "BUY" if last_success["tool"] == "buy" else "SELL"
                            tam = last_success["args"].get("amount", 0) * last_success["args"].get("price", 100)
                            erc8004_log_trade(tp, ta, tam)
                            time.sleep(2)  # Let nonce settle before next TX
                        # Post checkpoint every 3rd cycle to conserve gas (~0.001 ETH/checkpoint)
                        _erc8004_cycle_count = getattr(sys.modules[__name__], '_erc8004_cycle_count', 0) + 1
                        sys.modules[__name__]._erc8004_cycle_count = _erc8004_cycle_count
                        if trades_made or _erc8004_cycle_count % 3 == 0:
                            score = 80 if trades_made else 70
                            erc8004_log_checkpoint(summary[:200], cycle_action, cycle_pair, cycle_amount, cycle_price, score)
                        if trades_made:
                            log_action({"agent": "trader", "trades": trades_made, "summary": summary[:300]})
                            post_discord(f"[TRADER] {len(trades_made)} trades: {summary[:300]}")
                            # Write to cycles.jsonl for arena
                            cycle_entry = {
                                "timestamp": datetime.now().isoformat(),
                                "cycle": int(time.time()),
                                "model": TRADER_MODEL,
                                "action": "cycle_complete",
                                "duration_sec": round(time.time() - start, 1),
                                "risk": {"current_value": status_data.get("current_value",0), "starting_balance": 10000000, "unrealized_pnl": status_data.get("unrealized_pnl",0), "total_trades": status_data.get("total_trades",0)},
                                "agent_summary": summary[:300],
                                "agent_trades": trades_made,
                            }
                            with open(DATA_DIR / "cycles.jsonl", "a") as f:
                                f.write(json.dumps(cycle_entry) + "\n")
                        break
                    
                    result = execute_tool(name, args)
                    print(f"  [TRADER] {name}({json.dumps(args)[:80]}) → {result[:100]}")
                    
                    if name in ("buy", "sell"):
                        trades_made.append({"tool": name, "args": args, "result": result[:200]})
                    
                    messages.append({"role": "tool", "content": result})
                
                if name == "done":
                    break
            
            dur = time.time() - start
            print(f"  [TRADER {ts}] Cycle: {dur:.1f}s, {len(trades_made)} trades")
        
        except Exception as e:
            print(f"  [TRADER] Error: {e}")
        
        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))

# ─── Main ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║  ClawdTrader v3 — Lean Engine                                ║
║  SENTINEL (pure math) — every 15s — stops, scans, status    ║
║  TRADER  (nemotron)   — every 60s — decisions, trades       ║
║  152 tok/s • 2-3s responses • 20 pairs scanned              ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    threads = [
        threading.Thread(target=sentinel_loop, name="sentinel", daemon=True),
        threading.Thread(target=trader_loop, name="trader", daemon=True),
    ]
    
    for t in threads:
        t.start()
        print(f"  Started {t.name}")
    
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False
    
    print("\nEngine stopped.")
