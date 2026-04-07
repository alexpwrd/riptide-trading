"""
ClawdTrader v2 — Autonomous AI Trading Agent
Empowered agent: rich data, self-modification, minimal constraints.
"""

import json
import subprocess
import os
import sys
import shutil
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Any

# ─── Config ───────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_PORTS = {
    "qwen3.5:122b": 11434,
    "qwen3.5:35b": 11435,
    "nemotron-cascade-2": 11436,
}
MODEL = "qwen3.5:122b"
MAX_LOOPS = 50
LOOP_DELAY = 2
DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))
STRATEGY_FILE = DATA_DIR / "strategy.json"
PRISM_API_KEY = os.environ.get("PRISM_API_KEY", "")

# ─── Kraken CLI ───────────────────────────────────────────────
def _find_kraken() -> str:
    found = shutil.which("kraken")
    if found:
        return found
    cargo_path = os.path.expanduser("~/.cargo/bin/kraken")
    if os.path.isfile(cargo_path):
        return cargo_path
    return "kraken"

KRAKEN_BIN = _find_kraken()

def kraken_cmd(args: str) -> str:
    try:
        result = subprocess.run(
            [KRAKEN_BIN] + args.split(),
            capture_output=True, text=True, timeout=30
        )
        # Filter warning lines from output (e.g. DOGE ticker warnings)
        stdout = "\n".join(
            line for line in result.stdout.strip().split("\n")
            if not line.startswith("Warning:")
        ).strip()
        stderr = "\n".join(
            line for line in result.stderr.strip().split("\n")
            if not line.startswith("Warning:")
        ).strip()
        return stdout or stderr
    except subprocess.TimeoutExpired:
        return '{"error": "Command timed out"}'
    except Exception as e:
        return json.dumps({"error": str(e)})

# ─── Strategy (self-modifiable) ───────────────────────────────
DEFAULT_STRATEGY = {
    "description": "Adaptive multi-strategy trader",
    "max_position_pct": 40,
    "max_total_exposure_pct": 90,
    "pairs": ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "LINKUSD", "XDGUSD", "WIFUSD", "PEPEUSD", "FETUSD", "RENDERUSD", "SUIUSD", "JUPUSD", "ADAUSD"],
    "strategies_enabled": ["momentum", "mean_reversion", "breakout"],
    "entry_notes": "Use your judgment. No hardcoded rules — analyze the data and decide.",
    "exit_notes": "Set a target and stop-loss when entering. Hold until one is hit.",
    "lessons_learned": [
        "Breakeven exits killed v1 — set targets and honor them",
        "Fees were 45% of losses in v1 — now zero, so trade freely",
        "v1 only traded SOL because BTC/ETH ranges were too tight — use volatile pairs",
        "Re-evaluating every cycle causes anxiety trading — trust your thesis",
        "When a pair moves >1% in the last hour with rising volume, that is a MOMENTUM signal — size up and ride it",
        "Meme coins (WIF, PEPE, BONK) move 5-8% daily — use them for bigger swings",
        "Add to winners: if a position is up >1%, consider adding more rather than just holding",
        "Place limit buys 0.5-1% below market, not 2-3% — they fill more often"
    ]
}

def load_strategy() -> dict:
    if STRATEGY_FILE.exists():
        try:
            return json.loads(STRATEGY_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_STRATEGY.copy()

def save_strategy(strategy: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STRATEGY_FILE.write_text(json.dumps(strategy, indent=2))


# ─── Tools ────────────────────────────────────────────────────
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_snapshot",
            "description": "Get pre-computed prices for 13 pairs, updated every 15 seconds. MUCH FASTER than calling get_ticker for each pair. Use this FIRST to scan the market, then use get_ticker only for pairs you want to trade.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticker",
            "description": "Get real-time price data for a trading pair. Returns last price, bid, ask, 24h high/low, volume, open.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "Any Kraken USD pair. Top movers: FETUSD, JUPUSD, RENDERUSD, AVAXUSD, ADAUSD, WIFUSD, LINKUSD, SOLUSD, PEPEUSD, SUIUSD. Use XDGUSD for Doge (NOT DOGEUSD). You can trade ANY pair on Kraken."}
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_ohlc",
            "description": "Get OHLC candle data for trend analysis. Returns arrays of [timestamp, open, high, low, close, vwap, volume, count].",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "Trading pair"},
                    "interval": {"type": "integer", "description": "Candle interval in minutes. Options: 1, 5, 15, 60, 240, 1440. Default 60."}
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_orderbook",
            "description": "Get L2 order book — see buy/sell walls, support/resistance levels from real orders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "Trading pair"}
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "paper_buy",
            "description": "Place a paper BUY order. Use --type market for instant fill, or --type limit --price X to place at a specific price.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string"},
                    "amount": {"type": "number", "description": "Amount in base currency (e.g. 0.1 BTC, 5 ETH, 1000 WIF)"},
                    "order_type": {"type": "string", "description": "'market' for instant fill, 'limit' for specific price", "enum": ["market", "limit"]},
                    "price": {"type": "number", "description": "Limit price (required if order_type is limit)"}
                },
                "required": ["pair", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "paper_sell",
            "description": "Place a paper SELL order. Use --type market for instant fill, or --type limit --price X.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string"},
                    "amount": {"type": "number"},
                    "order_type": {"type": "string", "enum": ["market", "limit"]},
                    "price": {"type": "number", "description": "Limit price (required if order_type is limit)"}
                },
                "required": ["pair", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "Get full portfolio: cash, positions, open limit orders, trade history, P&L.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_orders",
            "description": "Get all pending limit orders.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "Cancel a pending limit order by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order ID to cancel"}
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "modify_strategy",
            "description": "Modify your own trading strategy. You can change position sizing, pairs, enabled strategies, entry/exit notes, or add lessons learned. Changes persist across cycles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {"type": "object", "description": "Key-value pairs to update in the strategy. e.g. {\"max_position_pct\": 30, \"pairs\": [\"BTCUSD\", \"AVAXUSD\"]}"},
                    "reason": {"type": "string", "description": "Why you are making this change"}
                },
                "required": ["updates", "reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_signals",
            "description": "Get AI-powered trading signals from PRISM — returns RSI, MACD, Bollinger bands, overall bullish/bearish direction and strength. Works best for major assets (BTC, ETH, SOL, AVAX, LINK, DOT, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Asset symbol without USD suffix e.g. BTC, ETH, SOL, AVAX"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_metrics",
            "description": "Get risk analytics from PRISM — daily/annual volatility, Sharpe ratio, Sortino ratio, max drawdown, current drawdown, beta. Use to compare which assets have the best risk-adjusted returns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Asset symbol e.g. BTC, ETH, FET, AVAX"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_stop_target",
            "description": "Set a stop-loss price and/or take-profit target price for a position. The sentinel checks these every 15 seconds and auto-executes. Use this instead of limit sell orders for stops.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string", "description": "Asset symbol e.g. BTC, ETH, SOL"},
                    "stop_price": {"type": "number", "description": "Stop-loss price — sentinel sells ALL if price drops below this"},
                    "target_price": {"type": "number", "description": "Take-profit price — sentinel sells HALF if price rises above this"}
                },
                "required": ["asset"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_code",
            "description": "Read a source code file for analysis. Use to understand how the system works and find improvements. Allowed files: clawdtrader.py, orchestrator.py, strategy.json",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File to read: clawdtrader.py, orchestrator.py, strategy.json"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_improvement",
            "description": "Propose a code improvement. Saved for human review — does NOT auto-apply. Use after reading code and finding something to fix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What the improvement does and why"},
                    "code_change": {"type": "string", "description": "The proposed code change (diff or new code)"},
                    "target_file": {"type": "string", "description": "Which file to modify"}
                },
                "required": ["description", "code_change", "target_file"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call when you have completed your analysis and any trades. Provide a summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What you did and why"}
                },
                "required": ["summary"]
            }
        }
    }
]

# ─── Tool Implementations ─────────────────────────────────────
def tool_get_ticker(pair: str) -> str:
    return kraken_cmd(f"ticker {pair} -o json")

def tool_get_ohlc(pair: str, interval: int = 60) -> str:
    raw = kraken_cmd(f"ohlc {pair} --interval {interval} -o json")
    try:
        data = json.loads(raw)
        for key, candles in data.items():
            if key == "last":
                continue
            # Return last 20 candles to keep context manageable
            recent = candles[-20:] if len(candles) > 20 else candles
            formatted = []
            for c in recent:
                ts, o, h, l, close, vwap, vol, count = c
                formatted.append({
                    "time": ts, "open": o, "high": h, "low": l,
                    "close": close, "volume": vol, "trades": count
                })
            return json.dumps(formatted, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e), "raw": raw[:200]})

def tool_get_orderbook(pair: str) -> str:
    raw = kraken_cmd(f"orderbook {pair} -o json")
    try:
        data = json.loads(raw)
        for key, book in data.items():
            asks = book.get("asks", [])[:10]
            bids = book.get("bids", [])[:10]
            return json.dumps({
                "asks": [{"price": a[0], "volume": a[1]} for a in asks],
                "bids": [{"price": b[0], "volume": b[1]} for b in bids],
                "spread": float(asks[0][0]) - float(bids[0][0]) if asks and bids else None
            }, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_paper_buy(pair: str, amount: float, order_type: str = "market", price: float = None) -> str:
    # ABSOLUTE BLOCK — defensive mode active
    import os as _os
    _sf = DATA_DIR / "strategy.json"
    if _sf.exists():
        import json as _json
        _s = _json.loads(_sf.read_text())
        if "DO NOT" in _s.get("entry_notes", "") or "DEFENSIVE" in _s.get("description", "").upper():
            return _json.dumps({"error": "TRADING BLOCKED — defensive mode active. Strategy says: " + _s.get("entry_notes", "")[:150]})
    # HARD BLOCK: enforce max exposure from strategy
    strategy_file = DATA_DIR / "strategy.json"
    if strategy_file.exists():
        try:
            strat = json.loads(strategy_file.read_text())
            max_exp = strat.get("max_total_exposure_pct", 90)
            status_raw = kraken_cmd("paper status -o json")
            bal_raw = kraken_cmd("paper balance -o json")
            st = json.loads(status_raw)
            bl = json.loads(bal_raw)
            total_val = st.get("current_value", 1000000)
            total_cash = bl.get("balances", {}).get("USD", {}).get("total", 0)
            deployed_pct = ((total_val - total_cash) / total_val * 100) if total_val > 0 else 0
            if deployed_pct > max_exp:
                msg = f"BLOCKED: {deployed_pct:.0f}% deployed exceeds {max_exp}% limit. SELL first."
                print(f"  [BUY BLOCKED] {msg}")
                return json.dumps({"error": msg})
        except Exception as e:
            print(f"  [BUY BLOCK ERROR] {e} — allowing buy as fallback")
    # Pre-check cash to avoid wasted tool calls on insufficient balance
    try:
        bal = json.loads(kraken_cmd("paper balance -o json"))
        cash = bal.get("balances", {}).get("USD", {}).get("available", 0)
        # Estimate cost
        if price:
            est_cost = amount * price
        else:
            ticker = json.loads(kraken_cmd(f"ticker {pair} -o json"))
            for k, v in ticker.items():
                est_cost = amount * float(v["a"][0])  # ask price
                break
            else:
                est_cost = 0
        if est_cost > cash * 1.01:  # 1% buffer
            return json.dumps({"error": f"Insufficient cash. Need ${est_cost:,.2f} but only ${cash:,.2f} available. Sell something first or reduce size."})
    except Exception:
        pass  # If pre-check fails, let kraken handle it
    cmd = f"paper buy {pair} {amount} --type {order_type} -o json"
    if order_type == "limit" and price:
        cmd = f"paper buy {pair} {amount} --type limit --price {price} -o json"
    return kraken_cmd(cmd)

def tool_paper_sell(pair: str, amount: float, order_type: str = "market", price: float = None) -> str:
    cmd = f"paper sell {pair} {amount} --type {order_type} -o json"
    if order_type == "limit" and price:
        cmd = f"paper sell {pair} {amount} --type limit --price {price} -o json"
    return kraken_cmd(cmd)

def tool_get_portfolio() -> str:
    status = kraken_cmd("paper status -o json")
    balance = kraken_cmd("paper balance -o json")
    history = kraken_cmd("paper history -o json")
    orders = kraken_cmd("paper orders -o json")
    parts = {}
    for name, raw in [("status", status), ("balance", balance), ("history", history), ("orders", orders)]:
        try:
            parts[name] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parts[name] = {"raw": raw[:200]}
    return json.dumps(parts, indent=1)

def tool_get_open_orders() -> str:
    return kraken_cmd("paper orders -o json")

def tool_cancel_order(order_id: str) -> str:
    return kraken_cmd(f"paper cancel {order_id} -o json")

def tool_modify_strategy(updates: dict, reason: str) -> str:
    # Check if strategy is locked
    current = load_strategy()
    if current.get("LOCKED"):
        return json.dumps({"error": "Strategy is LOCKED in defensive mode. Cannot modify. Wait for market reversal."})
    strategy = load_strategy()
    # Deep merge: append to lists instead of replacing them
    for key, value in updates.items():
        if isinstance(value, list) and isinstance(strategy.get(key), list):
            for item in value:
                if item not in strategy[key]:
                    strategy[key].append(item)
        else:
            strategy[key] = value
    if "modification_log" not in strategy:
        strategy["modification_log"] = []
    strategy["modification_log"].append({
        "timestamp": datetime.now().isoformat(),
        "changes": updates,
        "reason": reason
    })
    save_strategy(strategy)
    return json.dumps({"status": "strategy_updated", "reason": reason, "current_strategy": strategy})


def tool_get_signals(symbol: str) -> str:
    """Get AI trading signals from PRISM API — RSI, MACD, Bollinger bands, bullish/bearish."""
    if not PRISM_API_KEY:
        return json.dumps({"error": "PRISM_API_KEY not set"})
    try:
        import requests as req
        resp = req.get(
            f"https://api.prismapi.ai/signals/{symbol}",
            headers={"X-API-Key": PRISM_API_KEY},
            timeout=10
        )
        data = resp.json()
        signals = data.get("data", [])
        if signals:
            s = signals[0]
            return json.dumps({
                "symbol": symbol,
                "signal": s.get("overall_signal"),
                "direction": s.get("direction"),
                "strength": s.get("strength"),
                "price": s.get("current_price"),
                "indicators": s.get("indicators", {}),
                "active_signals": s.get("active_signals", []),
                "bullish_score": s.get("bullish_score"),
                "bearish_score": s.get("bearish_score"),
            }, indent=1)
        return json.dumps({"symbol": symbol, "warning": "No signals available for this asset"})
    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_get_risk_metrics(symbol: str) -> str:
    """Get risk metrics from PRISM — volatility, Sharpe, Sortino, drawdown, beta."""
    if not PRISM_API_KEY:
        return json.dumps({"error": "PRISM_API_KEY not set"})
    try:
        import requests as req
        resp = req.get(
            f"https://api.prismapi.ai/risk/{symbol}",
            headers={"X-API-Key": PRISM_API_KEY},
            timeout=10
        )
        return json.dumps(resp.json(), indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_market_snapshot() -> str:
    """Read pre-computed market snapshot — 13 pair prices updated every 15s by sentinel. No API calls needed."""
    ctx_file = DATA_DIR / "market_context.json"
    if ctx_file.exists():
        try:
            data = json.loads(ctx_file.read_text())
            # Add age info
            from datetime import datetime as dt
            ts = data.get("timestamp", "")
            if ts:
                age = (dt.now() - dt.fromisoformat(ts)).total_seconds()
                data["age_seconds"] = round(age)
            return json.dumps(data, indent=1)
        except Exception as e:
            return json.dumps({"error": str(e)})
    return json.dumps({"error": "No market context available yet"})


def tool_set_stop_target(asset: str, stop_price: float = 0, target_price: float = 0) -> str:
    """Set stop-loss and take-profit price levels for a position. Sentinel enforces these every 15s."""
    positions_file = DATA_DIR / "positions.json"
    positions = {}
    if positions_file.exists():
        try: positions = json.loads(positions_file.read_text())
        except: pass
    
    if asset not in positions:
        positions[asset] = {}
    if stop_price > 0:
        positions[asset]["stop_price"] = stop_price
    if target_price > 0:
        positions[asset]["target_price"] = target_price
    positions[asset]["updated"] = __import__("datetime").datetime.now().isoformat()
    
    positions_file.write_text(json.dumps(positions, indent=2))
    return json.dumps({"status": "ok", "asset": asset, "stop": stop_price, "target": target_price})


def tool_read_code(filename: str) -> str:
    """Read a source file for self-analysis. Only allows reading agent files."""
    allowed = ["clawdtrader.py", "orchestrator.py", "monitor.py", "sentinel.py", "strategy.json"]
    if filename not in allowed:
        return json.dumps({"error": f"Cannot read {filename}. Allowed: {allowed}"})
    try:
        base = os.path.dirname(__file__)
        if filename == "strategy.json":
            filepath = DATA_DIR / filename
        else:
            filepath = os.path.join(base, filename)
        content = open(filepath).read()
        # Truncate to avoid overwhelming context
        if len(content) > 5000:
            content = content[:2500] + "\n...TRUNCATED...\n" + content[-2500:]
        return json.dumps({"file": filename, "lines": content.count("\n"), "content": content})
    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_write_improvement(description: str, code_change: str, target_file: str) -> str:
    """Propose a code improvement. Writes to improvements.jsonl for human review. Does NOT auto-apply."""
    improvements_file = DATA_DIR / "improvements.jsonl"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "description": description,
        "target_file": target_file,
        "proposed_change": code_change[:2000],
        "status": "proposed"
    }
    with open(improvements_file, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return json.dumps({"status": "improvement_proposed", "description": description, "note": "Saved for human review. Will NOT auto-apply."})

TOOL_HANDLERS = {
    "get_market_snapshot": lambda args: tool_get_market_snapshot(),
    "get_ticker": lambda args: tool_get_ticker(args.get("pair", "BTCUSD")),
    "get_ohlc": lambda args: tool_get_ohlc(args.get("pair", "BTCUSD"), args.get("interval", 60)),
    "get_orderbook": lambda args: tool_get_orderbook(args.get("pair", "BTCUSD")),
    "paper_buy": lambda args: tool_paper_buy(args.get("pair"), args.get("amount"), args.get("order_type", "market"), args.get("price")),
    "paper_sell": lambda args: tool_paper_sell(args.get("pair"), args.get("amount"), args.get("order_type", "market"), args.get("price")),
    "get_portfolio": lambda args: tool_get_portfolio(),
    "get_open_orders": lambda args: tool_get_open_orders(),
    "cancel_order": lambda args: tool_cancel_order(args.get("order_id", "")),
    "get_signals": lambda args: tool_get_signals(args.get("symbol", "BTC")),
    "get_risk_metrics": lambda args: tool_get_risk_metrics(args.get("symbol", "BTC")),
    "read_code": lambda args: tool_read_code(args.get("filename", "")),
    "write_improvement": lambda args: tool_write_improvement(args.get("description", ""), args.get("code_change", ""), args.get("target_file", "")),
    "set_stop_target": lambda args: tool_set_stop_target(args.get("asset", ""), args.get("stop_price", 0), args.get("target_price", 0)),
    "modify_strategy": lambda args: tool_modify_strategy(args.get("updates", {}), args.get("reason", "")),
    "done": lambda args: json.dumps({"status": "done", "summary": args.get("summary", "")}),
}

# ─── Agent Loop ───────────────────────────────────────────────
def execute_tool(name: str, arguments: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return handler(arguments)
    except Exception as e:
        return json.dumps({"error": str(e)})

def agent_loop(task: str, model: str = MODEL) -> dict:
    result: dict[str, Any] = {"thinking": [], "actions": [], "trades": [], "summary": "", "turns": 0}

    strategy = load_strategy()

    print(f"\n{'='*60}")
    print(f"  ClawdTrader v2 — {model}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    system_prompt = f"""You are ClawdTrader v2, an autonomous AI trading agent with 10,000,000 USD in paper trading capital and ZERO fees.

YOUR CURRENT STRATEGY (you can modify this with modify_strategy):
{json.dumps(strategy, indent=2)}

TOOLS AVAILABLE:
- get_market_snapshot: pre-computed prices for 13 pairs (FAST — use this first to scan, then get_ticker only for trades)
- get_ticker: real-time price for a specific pair (slower, use only when needed)
- get_ohlc: candlestick charts (1m, 5m, 15m, 1h, 4h, 1d) for trend analysis
- get_orderbook: L2 order book to see buy/sell walls
- paper_buy / paper_sell: execute trades (market or limit orders)
- get_portfolio: see your positions, cash, P&L, pending orders
- get_open_orders / cancel_order: manage limit orders
- set_stop_target: set stop-loss and take-profit PRICES for the sentinel to enforce (checked every 15s)
- read_code: read system source files for self-analysis
- write_improvement: propose code improvements (saved for human review, not auto-applied)
- get_signals: AI trading signals (RSI, MACD, Bollinger bands, bullish/bearish) from PRISM
- get_risk_metrics: risk analytics (volatility, Sharpe ratio, drawdown) from PRISM
- modify_strategy: change your own trading parameters and approach
- done: finish this cycle with a summary

YOU HAVE 1 MILLION DOLLARS AND ZERO FEES. THE GOAL IS MAXIMUM PROFIT.

WHAT YOU CAN DO:
- BUY any of 750+ USD pairs (market or limit orders)
- SELL anything you hold (market or limit orders)
- Place LIMIT BUYS below market to catch dips — they auto-fill when price hits
- Place LIMIT SELLS above market as take-profit targets — they auto-fill when price hits
- Use OHLC candles (1m/5m/15m/1h/4h) to read trends
- Use orderbook to find real buy/sell walls
- Use modify_strategy to change your own approach in real time
- Trade volatile pairs: FET (8.8% range), JUP (8.6%), RENDER (7%), AVAX (6.9%), ADA (6.8%)

WHAT YOU CANNOT DO:
- NO short selling (you must own an asset to sell it)
- NO stop-loss orders (check prices each cycle, market sell if your stop is hit)
- NO leverage

STRATEGIES THAT WORK IN PAPER TRADING:
1. MOMENTUM: Buy assets moving up with volume. Ride the trend. Sell when momentum fades.
2. MEAN REVERSION: Buy dips at support, sell at resistance. Use orderbook walls as levels.
3. GRID TRADING: Place buy limits at multiple levels below price, sell limits above. Profit from range oscillation.
4. DCA ON DIPS: Buy more when price drops. Lower your average entry.
5. MULTI-PAIR ROTATION: Scan all pairs, concentrate in the best movers.

POSITION SIZING:
- Up to 40% on a single high-conviction trade
- Up to 90% total exposure
- When you spot momentum (>1% move + rising volume), SIZE UP
- Add to winners — if up >1%, buy more

ADAPT:
- If your strategy isn't working, use modify_strategy to change it
- Review your trade history each cycle — learn from what worked and what didn't

CRITICAL RULES:
- THERE ARE NO STOP-LOSS ORDERS. A limit sell BELOW current market price will FILL IMMEDIATELY as a market sell. To simulate a stop-loss, check the price each cycle and market sell if it drops below your mental stop level.
- Similarly, a limit buy ABOVE current market price fills immediately. Only use limit buys BELOW current price and limit sells ABOVE current price.
- Use XDGUSD (not DOGEUSD) for Dogecoin trading — DOGEUSD breaks the valuation system.
- You CANNOT short. You can only sell what you hold. When the market looks bearish, GO TO CASH and wait.
- You CAN trade highly volatile pairs (WIF, PEPE, BONK, AVAX) — these move 5-8% daily. Use them.

AVOID PAST MISTAKES:
- v1 exited every trade at breakeven because it re-evaluated R/R every cycle. DON'T DO THIS.
- When you enter a trade, commit to your thesis. Set a target and stop-loss and HOLD.
- v1 was paralyzed by conservative rules. You have no such constraints.
- v1 only traded SOL because it was the only volatile pair. You have 6+ pairs.
- v2 cycles 5-6 lost 12K by placing limit sells below market as "stop losses" — they filled instantly at bad prices. NEVER DO THIS."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task}
    ]

    for i in range(MAX_LOOPS):
        print(f"── Turn {i+1} ──")

        try:
            port = OLLAMA_PORTS.get(model, 11434)
            url = f"http://localhost:{port}/api/chat"
            resp = requests.post(url, json={
                "model": model,
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
                "stream": False,
            }, timeout=600)
            data = resp.json()
        except Exception as e:
            print(f"  LLM ERROR: {e}")
            break

        msg = data.get("message", {})
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])

        if content:
            display = content
            if "<think>" in display:
                parts = display.split("</think>")
                if len(parts) > 1:
                    display = parts[-1].strip()
                    think = parts[0].replace("<think>", "").strip()
                    if think:
                        print(f"  💭 {think[:200]}...")
            if display:
                print(f"  🤖 {display[:500]}")
                result["thinking"].append(display)

        if not tool_calls:
            result["turns"] = i + 1
            print(f"\n  ✅ Agent finished")
            break

        messages.append(msg)

        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            args = func.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    print(f"  ⚠️ Malformed args: {args[:100]}")
                    args = {}

            print(f"  🔧 {name}({json.dumps(args)[:150]})")

            if name == "done":
                summary = args.get("summary", "No summary")
                print(f"\n  ✅ DONE: {summary[:300]}")
                result["summary"] = summary
                result["turns"] = i + 1
                return result

            tool_result = execute_tool(name, args)

            if name in ("paper_buy", "paper_sell"):
                result["trades"].append({"tool": name, "args": args, "result": tool_result[:500]})
            if name == "modify_strategy":
                result["actions"].append({"tool": name, "reason": args.get("reason", "")})
            else:
                result["actions"].append({"tool": name, "args": args})

            display_result = tool_result[:200] + "..." if len(tool_result) > 200 else tool_result
            print(f"  📊 → {display_result}")

            messages.append({"role": "tool", "content": tool_result})

        time.sleep(LOOP_DELAY)

    result["turns"] = i + 1 if 'i' in dir() else 0
    print(f"\n{'='*60}")
    print(f"  Session complete — {result['turns']} turns")
    print(f"{'='*60}")
    return result

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ClawdTrader v2")
    parser.add_argument("task", nargs="*", default=["Scan all pairs. Find the best opportunities. Trade with conviction."])
    parser.add_argument("--model", "-m", default=MODEL)
    args = parser.parse_args()
    result = agent_loop(" ".join(args.task), model=args.model)
    if result.get("summary"):
        print(f"\nSummary: {result['summary']}")
