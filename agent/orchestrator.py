"""
ClawdTrader Multi-Agent Orchestrator
Runs multiple agents at different speeds with different models.

SENTINEL (9b)     — every 30s  — stop-loss/take-profit enforcement
TRADER (35b)      — every 3min — price scanning, trade execution
STRATEGIST (122b) — every 5min — deep analysis, OHLC, PRISM, rebalancing
JUDGE (235b)      — every 30min — strategy review, self-modification
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

DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
OLLAMA_URL = "http://localhost:11434/api/chat"
# Parallel Ollama instances on separate GPUs
OLLAMA_PORTS = {
    "qwen3.5:122b": 11434,     # GPU 0-3 (main instance)
    "qwen3.5:35b": 11435,      # GPU 5 (dedicated)
    "nemotron-cascade-2": 11436, # GPU 7 (dedicated)
}
KRAKEN_BIN = os.path.expanduser("~/.cargo/bin/kraken")
DISCORD_WEBHOOK = os.environ.get("CLAWDTRADER_DISCORD_WEBHOOK", "")

# --- Shared inter-agent files ---
SOLD_REGISTRY = DATA_DIR / "sold_assets.json"
MARKET_CONTEXT = DATA_DIR / "market_context.json"
POSITIONS_FILE = DATA_DIR / "positions.json"

def register_sold(asset, reason="stop-loss", cooldown_minutes=60):
    """Record that an asset was sold. Other agents should not re-buy within cooldown."""
    sold = {}
    if SOLD_REGISTRY.exists():
        try: sold = json.loads(SOLD_REGISTRY.read_text())
        except: pass
    sold[asset] = {
        "sold_at": datetime.now().isoformat(),
        "reason": reason,
        "cooldown_until": (datetime.now() + __import__("datetime").timedelta(minutes=cooldown_minutes)).isoformat()
    }
    SOLD_REGISTRY.write_text(json.dumps(sold, indent=2))

def is_banned(asset):
    """Check if an asset was recently sold and is in cooldown."""
    if not SOLD_REGISTRY.exists():
        return False
    try:
        sold = json.loads(SOLD_REGISTRY.read_text())
        entry = sold.get(asset, {})
        cooldown = entry.get("cooldown_until", "")
        if cooldown and datetime.fromisoformat(cooldown) > datetime.now():
            return True
    except: pass
    return False

def update_market_context(positions):
    """Write full market snapshot: positions + top pair prices."""
    try:
        # Position data
        pos_data = [{
            "asset": p["asset"], "price": p["price"], "pnl_pct": p["pnl_pct"],
            "value": p["value"], "change_24h": p["change_24h"]
        } for p in positions]
        
        # Scan key pairs for prices (fast, no LLM)
        scan_pairs = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "LINKUSD", 
                       "XDGUSD", "ADAUSD", "XRPUSD", "ALGOUSD", "FETUSD",
                       "WIFUSD", "RENDERUSD", "SUIUSD"]
        prices = {}
        for pair in scan_pairs:
            try:
                raw = kraken_cmd(f"ticker {pair} -o json")
                data = json.loads(raw)
                for k, v in data.items():
                    last = float(v["c"][0])
                    opn = float(v["o"])
                    chg = round((last - opn) / opn * 100, 2)
                    prices[pair] = {"price": last, "change_24h": chg}
            except:
                pass
        
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "positions": pos_data,
            "prices": prices,
        }
        MARKET_CONTEXT.write_text(json.dumps(snapshot, indent=1))
    except: pass

running = True
def signal_handler(sig, frame):
    global running
    print("\nShutting down all agents...")
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- Shared utilities ---
def kraken_cmd(args):
    try:
        r = subprocess.run([KRAKEN_BIN] + args.split(), capture_output=True, text=True, timeout=30)
        out = "\n".join(l for l in r.stdout.strip().split("\n") if not l.startswith("Warning:"))
        return out or r.stderr.strip()
    except subprocess.TimeoutExpired:
        return '{"error": "timeout"}'
    except Exception as e:
        return json.dumps({"error": str(e)})

def llm_call(model, system_prompt, user_prompt, tools=None, timeout=120):
    """Make an LLM call. Uses model-specific Ollama port for parallel GPU execution."""
    port = OLLAMA_PORTS.get(model, 11434)
    url = f"http://localhost:{port}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        return resp.json().get("message", {})
    except Exception as e:
        return {"content": f"Error: {e}"}

def get_positions():
    """Get positions with live prices and P&L."""
    bal = json.loads(kraken_cmd("paper balance -o json"))
    balances = bal.get("balances", {})
    cash = balances.get("USD", {}).get("available", 0)
    cash_reserved = balances.get("USD", {}).get("reserved", 0)

    hist = json.loads(kraken_cmd("paper history -o json"))
    costs = {}
    for t in hist.get("trades", []):
        asset = t["pair"].replace("USD", "")
        if asset not in costs:
            costs[asset] = {"buy": 0, "sell": 0}
        costs[asset][t["side"]] += float(t["cost"])

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
                change_24h = (price - float(v["o"])) / float(v["o"]) * 100
                mkt_val = price * total
                pnl = mkt_val - net_cost
                pnl_pct = (pnl / net_cost * 100) if net_cost > 0 else 0
                positions.append({
                    "asset": asset, "pair": pair, "amount": total,
                    "cost": net_cost, "value": mkt_val, "price": price,
                    "pnl": pnl, "pnl_pct": pnl_pct, "change_24h": change_24h
                })
        except Exception:
            pass
    return positions, cash, cash_reserved

def load_strategy():
    sf = DATA_DIR / "strategy.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {}

def log_action(agent, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logfile = DATA_DIR / "orchestrator.jsonl"
    entry = {"timestamp": datetime.now().isoformat(), "agent": agent, **data}
    with open(logfile, "a") as f:
        f.write(json.dumps(entry) + "\n")

def post_discord(content):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"username": "ClawdTrader", "content": content[:2000]}, timeout=10)
    except Exception:
        pass

def write_live_status(positions, cash):
    """Update live-status.json for the arena adapter."""
    try:
        status = json.loads(kraken_cmd("paper status -o json"))
        held = [p["asset"] for p in positions]
        live = {
            "starting_balance": status.get("starting_balance", 1000000),
            "current_value": status.get("current_value", 0),
            "unrealized_pnl": status.get("unrealized_pnl", 0),
            "unrealized_pnl_pct": round(status.get("unrealized_pnl", 0) / status.get("starting_balance", 1000000) * 100, 4),
            "drawdown_pct": status.get("unrealized_pnl_pct", 0),
            "total_trades": status.get("total_trades", 0),
            "pairs": "/".join(held),
            "cash": cash,
        }
        (DATA_DIR / "live-status.json").write_text(json.dumps(live))
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# SENTINEL — 9b, every 30s, stop-loss / take-profit enforcement
# ═══════════════════════════════════════════════════════════════
def sentinel_loop():
    interval = 15  # Check every 15 seconds — pure math, no LLM needed
    print(f"  [SENTINEL] Started — PURE MATH, every {interval}s (no LLM)")

    while running:
        start = time.time()
        try:
            positions, cash, _ = get_positions()
            write_live_status(positions, cash)
            update_market_context(positions)
            
            # Also merge WebSocket prices into market context if available
            ws_file = DATA_DIR / "ws_prices.json"
            if ws_file.exists():
                try:
                    ws_data = json.loads(ws_file.read_text())
                    ws_age = (__import__("datetime").datetime.now() - __import__("datetime").datetime.fromisoformat(ws_data.get("timestamp", "2000-01-01"))).total_seconds()
                    if ws_age < 30:  # Only use if <30s old
                        ctx_file = DATA_DIR / "market_context.json"
                        if ctx_file.exists():
                            ctx = json.loads(ctx_file.read_text())
                            ctx["ws_prices"] = ws_data.get("prices", {})
                            ctx["ws_age_seconds"] = round(ws_age)
                            ctx_file.write_text(json.dumps(ctx, indent=1))
                except Exception:
                    pass

            if not positions:
                time.sleep(interval)
                continue

            strategy = load_strategy()
            stop_pct = -2.5  # Default stop-loss
            profit_pct = 3.0  # Default take-profit
            cash_floor_pct = 20  # Keep 20% cash minimum

            ts = datetime.now().strftime("%H:%M:%S")
            status_line = " | ".join(f"{p['asset']}:{p['pnl_pct']:+.1f}%" for p in positions)
            print(f"  [SENTINEL {ts}] Cash:${cash:,.0f} | {status_line}")

            # Load position-level stops/targets set by trader/strategist
            pos_levels = {}
            pos_file = DATA_DIR / "positions.json"
            if pos_file.exists():
                try: pos_levels = json.loads(pos_file.read_text())
                except: pass

            acted = False
            for p in positions:
                asset = p["asset"]
                pair = p["pair"]
                amount = p["amount"]
                pnl_pct = p["pnl_pct"]
                price = p["price"]

                # Check position-level price stops FIRST (more precise than %)
                levels = pos_levels.get(asset, {})
                stop_price = levels.get("stop_price", 0)
                target_price = levels.get("target_price", 0)

                if stop_price > 0 and price <= stop_price:
                    print(f"  [SENTINEL] PRICE STOP {asset} at ${price:,.2f} <= ${stop_price:,.2f} — SELLING ALL")
                    result = kraken_cmd(f"paper sell {pair} {amount} --type market -o json")
                    print(f"  [SENTINEL] SOLD: {result[:150]}")
                    register_sold(asset, f"price stop at ${price:,.2f}")
                    post_discord(f"[SENTINEL] PRICE STOP: Sold all {amount:,.0f} {asset} at ${price:,.2f} (stop was ${stop_price:,.2f})")
                    log_action("sentinel", {"action": "price_stop", "asset": asset, "amount": amount, "price": price, "stop_price": stop_price})
                    acted = True
                    continue

                if target_price > 0 and price >= target_price:
                    sell_amount = amount * 0.5
                    print(f"  [SENTINEL] PRICE TARGET {asset} at ${price:,.2f} >= ${target_price:,.2f} — SELLING HALF")
                    result = kraken_cmd(f"paper sell {pair} {sell_amount} --type market -o json")
                    print(f"  [SENTINEL] SOLD HALF: {result[:150]}")
                    post_discord(f"[SENTINEL] TARGET HIT: Sold {sell_amount:,.0f} {asset} at ${price:,.2f} (+target ${target_price:,.2f})")
                    log_action("sentinel", {"action": "price_target", "asset": asset, "amount": sell_amount, "price": price, "target_price": target_price})
                    acted = True
                    continue

                # STOP-LOSS — percentage-based fallback
                if pnl_pct <= stop_pct:
                    print(f"  [SENTINEL] STOP-LOSS {asset} at {pnl_pct:.2f}% — SELLING ALL")
                    result = kraken_cmd(f"paper sell {pair} {amount} --type market -o json")
                    print(f"  [SENTINEL] SOLD: {result[:150]}")
                    register_sold(asset, f"stop-loss at {pnl_pct:.2f}%")
                    post_discord(f"[SENTINEL] STOP-LOSS: Sold all {amount:,.0f} {asset} at {pnl_pct:.2f}%")
                    log_action("sentinel", {"action": "stop_loss", "asset": asset, "amount": amount, "pnl_pct": pnl_pct})
                    acted = True

                # TAKE-PROFIT — sell half at +3%
                elif pnl_pct >= profit_pct:
                    sell_amount = amount * 0.5
                    print(f"  [SENTINEL] TAKE-PROFIT {asset} at {pnl_pct:.2f}% — SELLING HALF")
                    result = kraken_cmd(f"paper sell {pair} {sell_amount} --type market -o json")
                    print(f"  [SENTINEL] SOLD HALF: {result[:150]}")
                    post_discord(f"[SENTINEL] TAKE-PROFIT: Sold {sell_amount:,.0f} {asset} at +{pnl_pct:.2f}%")
                    log_action("sentinel", {"action": "take_profit", "asset": asset, "amount": sell_amount, "pnl_pct": pnl_pct})
                    acted = True

            if not acted:
                log_action("sentinel", {"action": "hold", "status": status_line})

            # CASH FLOOR ENFORCEMENT — cancel buy orders if cash too low
            try:
                status_data = json.loads(kraken_cmd("paper status -o json"))
                total_val = status_data.get("current_value", 1000000)
                cash_floor = total_val * cash_floor_pct / 100
                
                bal_data = json.loads(kraken_cmd("paper balance -o json"))
                total_cash = bal_data.get("balances", {}).get("USD", {}).get("total", 0)
                
                if total_cash < cash_floor:
                    orders_data = json.loads(kraken_cmd("paper orders -o json"))
                    buy_orders = [o for o in orders_data.get("open_orders", []) if o["side"] == "buy"]
                    # Cancel cheapest buy orders until cash is above floor
                    buy_orders.sort(key=lambda o: o.get("price", 0))  # cheapest first
                    for o in buy_orders:
                        if total_cash >= cash_floor:
                            break
                        oid = o["id"]
                        reserved = o.get("reserved_amount", 0)
                        kraken_cmd(f"paper cancel {oid} -o json")
                        total_cash += reserved
                        print(f"  [SENTINEL] Cash floor: cancelled {oid} {o['pair']} buy, freed ${reserved:,.0f}")
                        log_action("sentinel", {"action": "cash_floor", "cancelled": oid, "freed": reserved})
            except Exception:
                pass

        except Exception as e:
            print(f"  [SENTINEL] Error: {e}")

        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))

# ═══════════════════════════════════════════════════════════════
# TRADER — 35b, every 3min, active trading
# ═══════════════════════════════════════════════════════════════
def trader_loop():
    model = "qwen3.5:35b"
    interval = 180
    print(f"  [TRADER] Started — {model}, every {interval}s")
    time.sleep(10)  # Stagger start

    # Import the agent loop from clawdtrader
    sys.path.insert(0, os.path.dirname(__file__))
    from clawdtrader import agent_loop, load_strategy as load_strat

    while running:
        start = time.time()
        try:
            positions, cash, cash_reserved = get_positions()
            strategy = load_strat()
            status = json.loads(kraken_cmd("paper status -o json"))

            pos_text = "\n".join(f"  {p['asset']}: {p['amount']:,.0f} units, cost=${p['cost']:,.0f}, value=${p['value']:,.0f}, PNL={p['pnl_pct']:+.2f}%, 24h={p['change_24h']:+.1f}%" for p in positions)
            holdings_text = f"CASH: ${cash:,.2f} available, ${cash_reserved:,.2f} reserved\nHOLDINGS:\n{pos_text}" if positions else f"CASH: ${cash:,.2f}\nHOLDINGS: None"

            # Check banned assets
            banned = []
            if SOLD_REGISTRY.exists():
                try:
                    sold = json.loads(SOLD_REGISTRY.read_text())
                    for asset, info in sold.items():
                        if is_banned(asset):
                            banned.append(f"{asset} (sold: {info.get('reason','?')})")
                except: pass
            banned_text = "\nBANNED (recently stopped out, do NOT buy): " + ", ".join(banned) if banned else ""

            # Read scanner opportunities
            opp_text = ""
            opp_file = DATA_DIR / "opportunities.json"
            if opp_file.exists():
                try:
                    opp = json.loads(opp_file.read_text())
                    top = opp.get("top_movers", [])[:5]
                    sentiment = opp.get("market_sentiment", "?")
                    if top:
                        opp_lines = [f"  {m['pair']}: {m['change_24h']:+.1f}% (range: {m['range_pct']:.1f}%)" for m in top]
                        opp_text = f"\nSCANNER ({sentiment}): " + " | ".join(f"{m['pair']} {m['change_24h']:+.1f}%" for m in top[:5])
                except Exception:
                    pass

            prompt = f"""[TRADER CYCLE] {datetime.now().strftime('%H:%M:%S')}{banned_text}{opp_text}

Portfolio: ${status.get('current_value', 0):,.2f} (PNL: ${status.get('unrealized_pnl', 0):,.2f})
{holdings_text}

Strategy: {json.dumps(strategy, indent=1)}

Quick scan: check prices on your top pairs. If momentum is strong, trade. If a position is losing, cut it. Be fast and decisive. The sentinel handles stop-losses — you handle opportunity."""

            print(f"  [TRADER {datetime.now().strftime('%H:%M:%S')}] Starting cycle...")
            result = agent_loop(prompt, model=model)
            summary = (result or {}).get("summary", "")[:200]
            trades = len((result or {}).get("trades", []))
            print(f"  [TRADER] Done: {trades} trades, {summary[:100]}")

            log_action("trader", {"summary": summary, "trades": trades})

            # Write to cycles.jsonl for the adapter
            risk = {
                "ok": True,
                "starting_balance": status.get("starting_balance", 1000000),
                "current_value": status.get("current_value", 0),
                "unrealized_pnl": status.get("unrealized_pnl", 0),
                "drawdown_pct": round(abs(status.get("unrealized_pnl_pct", 0)), 2),
                "total_trades": status.get("total_trades", 0),
                "open_orders": status.get("open_orders", 0),
            }
            cycle_entry = {
                "timestamp": datetime.now().isoformat(),
                "cycle": int(time.time()),
                "model": model,
                "action": "cycle_complete",
                "duration_sec": round(time.time() - start, 1),
                "risk": risk,
                "agent_summary": summary,
                "agent_trades": (result or {}).get("trades", []),
            }
            with open(DATA_DIR / "cycles.jsonl", "a") as f:
                f.write(json.dumps(cycle_entry) + "\n")

            if trades > 0 and DISCORD_WEBHOOK:
                post_discord(f"[TRADER] {trades} trades: {summary[:300]}")

        except Exception as e:
            print(f"  [TRADER] Error: {e}")

        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))

# ═══════════════════════════════════════════════════════════════
# STRATEGIST — 122b, every 5min, deep analysis
# ═══════════════════════════════════════════════════════════════
def strategist_loop():
    model = "qwen3.5:122b"
    interval = 300
    print(f"  [STRATEGIST] Started — {model}, every {interval}s")
    time.sleep(30)  # Stagger start

    sys.path.insert(0, os.path.dirname(__file__))
    from clawdtrader import agent_loop, load_strategy as load_strat

    while running:
        start = time.time()
        try:
            positions, cash, cash_reserved = get_positions()
            strategy = load_strat()
            status = json.loads(kraken_cmd("paper status -o json"))

            pos_text = "\n".join(f"  {p['asset']}: {p['amount']:,.0f} units, cost=${p['cost']:,.0f}, value=${p['value']:,.0f}, PNL={p['pnl_pct']:+.2f}%, 24h={p['change_24h']:+.1f}%" for p in positions)
            holdings_text = f"CASH: ${cash:,.2f} available, ${cash_reserved:,.2f} reserved\nHOLDINGS:\n{pos_text}" if positions else f"CASH: ${cash:,.2f}\nHOLDINGS: None"

            # Check banned assets
            banned = []
            if SOLD_REGISTRY.exists():
                try:
                    sold = json.loads(SOLD_REGISTRY.read_text())
                    for asset, info in sold.items():
                        if is_banned(asset):
                            banned.append(f"{asset} (sold: {info.get('reason','?')})")
                except: pass
            banned_text = "\nBANNED (recently stopped out, do NOT buy): " + ", ".join(banned) if banned else ""

            prompt = f"""[STRATEGIST DEEP ANALYSIS] {datetime.now().strftime('%H:%M:%S')}{banned_text}

Portfolio: ${status.get('current_value', 0):,.2f} (PNL: ${status.get('unrealized_pnl', 0):,.2f})
{holdings_text}

Strategy: {json.dumps(strategy, indent=1)}

Do a DEEP analysis:
1. Use get_ohlc for 1h and 4h candles on your positions
2. Use get_orderbook to find support/resistance walls
3. Use get_signals from PRISM for RSI/MACD
4. Decide: should we rotate positions? Add to winners? Cut losers?
5. Place limit orders at key levels
6. If the strategy needs updating, use modify_strategy

Think deeply. Take your time. Quality over speed."""

            print(f"  [STRATEGIST {datetime.now().strftime('%H:%M:%S')}] Starting deep analysis...")
            result = agent_loop(prompt, model=model)
            summary = (result or {}).get("summary", "")[:300]
            trades = len((result or {}).get("trades", []))
            print(f"  [STRATEGIST] Done: {trades} trades, {summary[:150]}")

            log_action("strategist", {"summary": summary, "trades": trades})

            # Write to cycles.jsonl
            risk = {
                "ok": True,
                "starting_balance": status.get("starting_balance", 1000000),
                "current_value": status.get("current_value", 0),
                "unrealized_pnl": status.get("unrealized_pnl", 0),
                "drawdown_pct": round(abs(status.get("unrealized_pnl_pct", 0)), 2),
                "total_trades": status.get("total_trades", 0),
                "open_orders": status.get("open_orders", 0),
            }
            cycle_entry = {
                "timestamp": datetime.now().isoformat(),
                "cycle": int(time.time()),
                "model": model,
                "action": "cycle_complete",
                "duration_sec": round(time.time() - start, 1),
                "risk": risk,
                "agent_summary": summary,
                "agent_trades": (result or {}).get("trades", []),
            }
            with open(DATA_DIR / "cycles.jsonl", "a") as f:
                f.write(json.dumps(cycle_entry) + "\n")

            if DISCORD_WEBHOOK:
                post_discord(f"[STRATEGIST] {summary[:500]}")

        except Exception as e:
            print(f"  [STRATEGIST] Error: {e}")

        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))



# ═══════════════════════════════════════════════════════════════
# FAST TRADER — nemotron-cascade-2, every 90s, quick opportunities
# ═══════════════════════════════════════════════════════════════
def fast_trader_loop():
    model = "nemotron-cascade-2"
    interval = 90
    print(f"  [FAST-TRADER] Started — {model}, every {interval}s")
    time.sleep(20)  # Stagger start

    sys.path.insert(0, os.path.dirname(__file__))
    from clawdtrader import agent_loop, load_strategy as load_strat

    while running:
        start = time.time()
        try:
            positions, cash, cash_reserved = get_positions()
            strategy = load_strat()
            status_data = json.loads(kraken_cmd("paper status -o json"))

            # Read market context instead of scanning
            market_ctx = ""
            ctx_file = DATA_DIR / "market_context.json"
            if ctx_file.exists():
                try:
                    ctx = json.loads(ctx_file.read_text())
                    prices = ctx.get("prices", {})
                    if prices:
                        lines = []
                        for pair, data in sorted(prices.items(), key=lambda x: abs(x[1].get("change_24h", 0)), reverse=True):
                            lines.append(f"  {pair}: ${data['price']:,.4f} ({data['change_24h']:+.1f}%)")
                        market_ctx = "MARKET SNAPSHOT (pre-computed, <15s old):\n" + "\n".join(lines)
                except: pass

            pos_text = "\n".join(f"  {p['asset']}: {p['amount']:,.0f} PNL={p['pnl_pct']:+.1f}%" for p in positions) if positions else "  None"

            prompt = f"""[FAST TRADER] {datetime.now().strftime('%H:%M:%S')}

Portfolio: ${status_data.get('current_value', 0):,.2f} | Cash: ${cash:,.2f}
Positions:
{pos_text}

{market_ctx}

Be FAST. Use get_market_snapshot to check prices. If you see a pair moving >1% in our favor, buy it. If a position is losing >2%, sell it. Quick decisions only. Call done when finished."""

            print(f"  [FAST-TRADER {datetime.now().strftime('%H:%M:%S')}] Scanning...")
            result = agent_loop(prompt, model=model)
            trades = len((result or {}).get("trades", []))
            summary = (result or {}).get("summary", "")[:150]
            if trades > 0:
                print(f"  [FAST-TRADER] {trades} trades: {summary}")
                log_action("fast_trader", {"summary": summary, "trades": trades})
                # Write to cycles.jsonl
                cycle_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "cycle": int(time.time()),
                    "model": model,
                    "action": "cycle_complete",
                    "duration_sec": round(time.time() - start, 1),
                    "risk": {"current_value": status_data.get("current_value", 0),
                             "starting_balance": status_data.get("starting_balance", 1000000),
                             "unrealized_pnl": status_data.get("unrealized_pnl", 0)},
                    "agent_summary": summary,
                    "agent_trades": (result or {}).get("trades", []),
                }
                with open(DATA_DIR / "cycles.jsonl", "a") as f:
                    f.write(json.dumps(cycle_entry) + "\n")
            else:
                print(f"  [FAST-TRADER] No trades — {summary[:80]}")

        except Exception as e:
            print(f"  [FAST-TRADER] Error: {e}")

        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))



# ═══════════════════════════════════════════════════════════════
# SCANNER — qwen3.5:27b, every 60s, opportunity detection
# ═══════════════════════════════════════════════════════════════
def scanner_loop():
    interval = 60
    print(f"  [SCANNER] Started — pure code scan + 27b analysis, every {interval}s")
    time.sleep(5)
    
    SCAN_PAIRS = ["BTCUSD","ETHUSD","SOLUSD","AVAXUSD","ALGOUSD","LINKUSD",
                  "XRPUSD","ADAUSD","FETUSD","SUIUSD","WIFUSD","RENDERUSD",
                  "JUPUSD","PEPEUSD","BONKUSD","ONDOUSD","TRUMPUSD","KASUSD","TAOUSD","ARBUSD"]
    
    while running:
        start = time.time()
        try:
            # Scan all pairs — pure code, no LLM
            movers = []
            for pair in SCAN_PAIRS:
                try:
                    raw = kraken_cmd(f"ticker {pair} -o json")
                    data = json.loads(raw)
                    for k, v in data.items():
                        price = float(v["c"][0])
                        opn = float(v["o"])
                        chg = (price - opn) / opn * 100 if opn > 0 else 0
                        vol = float(v["v"][1])
                        high = float(v["h"][1])
                        low = float(v["l"][1])
                        rng = (high - low) / low * 100 if low > 0 else 0
                        movers.append({
                            "pair": pair, "price": price, "change_24h": round(chg, 2),
                            "volume": vol, "range_pct": round(rng, 2),
                            "range_pos": round((price - low) / (high - low) * 100, 1) if high > low else 50
                        })
                except Exception:
                    pass
            
            # Rank by absolute change
            movers.sort(key=lambda x: abs(x["change_24h"]), reverse=True)
            
            # Write opportunities file
            opportunities = {
                "timestamp": datetime.now().isoformat(),
                "top_movers": movers[:10],
                "positive_count": sum(1 for m in movers if m["change_24h"] > 0),
                "total_scanned": len(movers),
                "best_momentum": movers[0] if movers else None,
                "market_sentiment": "bullish" if sum(1 for m in movers if m["change_24h"] > 0) > len(movers) * 0.6 else "bearish" if sum(1 for m in movers if m["change_24h"] > 0) < len(movers) * 0.4 else "neutral"
            }
            (DATA_DIR / "opportunities.json").write_text(json.dumps(opportunities, indent=1))
            
            ts = datetime.now().strftime("%H:%M:%S")
            top = movers[0] if movers else {"pair": "?", "change_24h": 0}
            sentiment = opportunities["market_sentiment"]
            pos_count = opportunities["positive_count"]
            print(f"  [SCANNER {ts}] {pos_count}/{len(movers)} green | Top: {top['pair']} {top['change_24h']:+.1f}% | Sentiment: {sentiment}")
            
        except Exception as e:
            print(f"  [SCANNER] Error: {e}")
        
        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))



# ═══════════════════════════════════════════════════════════════
# JUDGE — qwen3:235b, every 30min, strategy review
# ═══════════════════════════════════════════════════════════════
def judge_loop():
    model = "qwen3.5:122b"
    interval = 1800  # 30 minutes
    print(f"  [JUDGE] Started — {model}, every {interval//60}min")
    time.sleep(120)  # Let other agents run first
    
    while running:
        start = time.time()
        try:
            # Gather all data for review
            status_raw = kraken_cmd("paper status -o json")
            status = json.loads(status_raw)
            
            strategy = load_strategy()
            
            # Read orchestrator action log
            orch_path = DATA_DIR / "orchestrator.jsonl"
            actions = []
            if orch_path.exists():
                lines = orch_path.read_text().strip().split("\n")
                for line in lines[-50:]:
                    try:
                        actions.append(json.loads(line))
                    except: pass
            
            # Count actions by agent
            agent_counts = {}
            stop_count = 0
            for a in actions:
                agent = a.get("agent", "?")
                agent_counts[agent] = agent_counts.get(agent, 0) + 1
                if a.get("action") == "stop_loss":
                    stop_count += 1
            
            # Read opportunities
            opp_path = DATA_DIR / "opportunities.json"
            opportunities = {}
            if opp_path.exists():
                try:
                    opportunities = json.loads(opp_path.read_text())
                except: pass
            
            prompt = f"""[JUDGE REVIEW] {datetime.now().strftime('%Y-%m-%d %H:%M')}

PORTFOLIO: ${status.get('current_value', 0):,.2f} (PNL: ${status.get('unrealized_pnl', 0):,.2f} / {status.get('unrealized_pnl_pct', 0):.2f}%)
Trades: {status.get('total_trades', 0)} | Orders: {status.get('open_orders', 0)}

CURRENT STRATEGY:
{json.dumps(strategy, indent=1)}

RECENT ACTIONS (last 50):
Agent activity: {json.dumps(agent_counts)}
Stop-losses triggered: {stop_count}

MARKET OPPORTUNITIES:
{json.dumps(opportunities, indent=1) if opportunities else 'No scanner data'}

YOUR ROLE: You are the JUDGE. Review everything and decide:
1. Is the current strategy working? What should change?
2. Should we unlock trading? (Currently locked={strategy.get('LOCKED', False)})
3. What lessons should be added?
4. What position sizing and stop levels are appropriate?

If you want to change the strategy, use modify_strategy.
If the strategy should stay locked, explain why.
Call done with your verdict."""

            print(f"  [JUDGE {datetime.now().strftime('%H:%M:%S')}] Starting review...")
            
            sys.path.insert(0, os.path.dirname(__file__))
            from clawdtrader import agent_loop as judge_agent_loop
            
            result = judge_agent_loop(prompt, model=model)
            summary = (result or {}).get("summary", "")[:300]
            print(f"  [JUDGE] Verdict: {summary[:200]}")
            
            log_action("judge", {"summary": summary})
            
            if DISCORD_WEBHOOK:
                post_discord(f"[JUDGE] {summary[:500]}")
                
        except Exception as e:
            print(f"  [JUDGE] Error: {e}")
        
        elapsed = time.time() - start
        time.sleep(max(1, interval - elapsed))

# ═══════════════════════════════════════════════════════════════
# MAIN — Launch all agents as threads
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║  ClawdTrader Multi-Agent Orchestrator                        ║
║  SENTINEL  (9b)   — every 30s  — stops & take-profits        ║
║  TRADER    (35b)  — every 3min — active trading               ║
║  SCANNER    (code)  — every 60s  — scan 20 pairs               ║
║  FAST-TRADER(nemotron) — every 90s — quick opportunities       ║
║  STRATEGIST(122b) — every 5min — deep analysis                ║
║  JUDGE     (122b) — every 30min — strategy review              ║
╚══════════════════════════════════════════════════════════════╝
""")

    threads = [
        threading.Thread(target=sentinel_loop, name="sentinel", daemon=True),
        threading.Thread(target=scanner_loop, name="scanner", daemon=True),
        threading.Thread(target=fast_trader_loop, name="fast_trader", daemon=True),
        threading.Thread(target=trader_loop, name="trader", daemon=True),
        threading.Thread(target=strategist_loop, name="strategist", daemon=True),
        threading.Thread(target=judge_loop, name="judge", daemon=True),
    ]

    for t in threads:
        t.start()
        print(f"  Started {t.name}")

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False

    print("\nAll agents stopped.")
