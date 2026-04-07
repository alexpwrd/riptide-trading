# Riptide 🌊

**Autonomous multi-agent AI trading system** built for the [AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/ai-trading-agents) (March 30 – April 12, 2026).

Riptide uses multiple LLM models orchestrated in real-time to analyze crypto markets, execute trades via Kraken CLI, and post every decision on-chain via ERC-8004 for transparent, trustless verification.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Riptide Engine                     │
│                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │ SENTINEL  │  │   TRADER     │  │   REPORTER    │ │
│  │ pure math │  │ nemotron LLM │  │  phi4-mini    │ │
│  │ every 15s │  │  every 60s   │  │  every 10min  │ │
│  │           │  │              │  │               │ │
│  │ • stops   │  │ • OHLC       │  │ • Discord     │ │
│  │ • profits │  │ • orderbook  │  │   updates     │ │
│  │ • scans   │  │ • decisions  │  │               │ │
│  └─────┬─────┘  └──────┬───────┘  └───────────────┘ │
│        │               │                             │
│        ▼               ▼                             │
│  ┌──────────────────────────────┐                   │
│  │     Kraken CLI (paper)       │                   │
│  │  Real market data + trades   │                   │
│  └──────────────────────────────┘                   │
│        │               │                             │
│        ▼               ▼                             │
│  ┌──────────────────────────────┐                   │
│  │   ERC-8004 On-Chain Layer    │                   │
│  │  Sepolia Testnet             │                   │
│  │                              │                   │
│  │  • AgentRegistry (identity)  │                   │
│  │  • RiskRouter (trade intents)│                   │
│  │  • ValidationRegistry        │                   │
│  │    (checkpoints + scoring)   │                   │
│  └──────────────────────────────┘                   │
└─────────────────────────────────────────────────────┘
```

## Key Features

- **Multi-agent orchestration** — Sentinel (pure math, 15s) + Trader (LLM, 60s) + Reporter (Discord, 10min)
- **Real Kraken CLI integration** — Live market data (tickers, OHLC, L2 orderbooks), paper trading execution
- **Self-modifying strategy** — Agent rewrites its own `strategy.json` based on performance
- **On-chain transparency** — Every trade is an EIP-712 signed TradeIntent submitted to the RiskRouter
- **Validation checkpoints** — PostAttestation after every cycle for verifiable decision audit trail
- **20 crypto pairs** — BTC, ETH, SOL, AVAX, ALGO, LINK, ADA, FET, WIF, PEPE, RENDER, JUP, and more
- **Risk controls** — Sentinel enforces -2.5% stop-loss and +3% take-profit every 15 seconds

## On-Chain Identity (ERC-8004)

| Field | Value |
|---|---|
| **Agent Name** | Riptide |
| **Agent ID** | 24 |
| **Network** | Sepolia Testnet (Chain ID: 11155111) |
| **Operator** | `0x075DC84Ca3FAC21BE7c2eEA1901A04417f0dC6C6` |
| **Agent Wallet** | `0x15d6f7cbBDc6f410285174DC64164dB703d1fF30` |

### Shared Hackathon Contracts (Sepolia)

| Contract | Address |
|---|---|
| AgentRegistry | `0x97b07dDc405B0c28B17559aFFE63BdB3632d0ca3` |
| HackathonVault | `0x0E7CD8ef9743FEcf94f9103033a044caBD45fC90` |
| RiskRouter | `0xd6A6952545FF6E6E6681c2d15C59f9EB8F40FdBC` |
| ReputationRegistry | `0x423a9904e39537a9997fbaF0f220d79D7d545763` |
| ValidationRegistry | `0x92bF63E5C7Ac6980f237a7164Ab413BE226187F1` |

## Tech Stack

- **LLM Models**: nemotron-cascade-2 (trading), phi4-mini (reporting) — running on 8x NVIDIA A100-SXM4-80GB
- **Trading**: [Kraken CLI](https://github.com/krakenfx/kraken-cli) v0.2.3 (Rust binary, paper trading mode)
- **Market Data**: [PRISM API](https://prismapi.ai) for multi-asset resolution
- **On-Chain**: web3.py + EIP-712 typed data signatures on Sepolia
- **Infrastructure**: Azure ML A100 cluster, Docker, Cloudflare Tunnel

## File Structure

```
agent/
  engine.py          — v3 Lean Engine (Sentinel + Trader loops)
  erc8004.py         — ERC-8004 on-chain integration (register, trade, checkpoint)
  orchestrator.py    — v2 Multi-agent orchestrator (4 models)
  clawdtrader.py     — v1 Original single-agent trader
  reporter.py        — Discord reporter (phi4-mini)
  sentinel.py        — Standalone sentinel module
  ws_streamer.py     — WebSocket price streamer
strategy.json        — Self-modifying trading strategy
Dockerfile           — Container image with Kraken CLI + Foundry + Python
docker-compose.yml   — Full stack deployment
PERFORMANCE_LOG.md   — 243 check-ins documenting the agent's evolution
```

## Quick Start

```bash
# 1. Install Kraken CLI
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

# 2. Initialize paper trading
kraken paper init --balance 10000000 --currency USD

# 3. Install Python dependencies
pip install web3 requests

# 4. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 5. Run the engine
cd agent && python3 engine.py
```

## Evolution

Riptide evolved through 5 phases over 5 days of continuous autonomous operation:

1. **v1 — Single Agent** ($10K): Conservative analyst, 45% of losses from fees
2. **v2 — Self-Modifying** ($100K): Added OHLC, orderbook, limit orders, strategy self-modification
3. **v3 — Multi-Agent** ($1M): Sentinel (9b) + Trader (35b) + Strategist (122b) + Fast-trader
4. **v4 — Lean Engine** ($10M): Simplified to Sentinel + Trader, 20 pairs, aggressive momentum
5. **v5 — On-Chain** ($10M): Added ERC-8004 integration for trustless verification

Full journey documented in [PERFORMANCE_LOG.md](PERFORMANCE_LOG.md) — 243 check-ins, 70+ improvement ideas.

## License

MIT
