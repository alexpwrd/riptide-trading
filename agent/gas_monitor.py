"""
Gas Monitor for Riptide — checks operator wallet balance every 30 min.
Warns on Discord if low, pauses on-chain submissions if critical.
"""

import json
import os
import sys
import time
import requests
from pathlib import Path
from datetime import datetime

from web3 import Web3

# --- Config ---
def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                if val and key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()

load_env()

RPC_URL = os.environ.get("SEPOLIA_RPC_URL", "https://ethereum-sepolia-rpc.publicnode.com")
OPERATOR_ADDR = os.environ.get("OPERATOR_ADDRESS", "")
DISCORD_WEBHOOK = os.environ.get("CLAWDTRADER_DISCORD_WEBHOOK", "")
DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))

PAUSE_FILE = DATA_DIR / "erc8004_paused"
WARN_THRESHOLD = 0.02   # ETH — post Discord warning
PAUSE_THRESHOLD = 0.005  # ETH — pause on-chain submissions
CHECK_INTERVAL = 1800    # 30 minutes

w3 = Web3(Web3.HTTPProvider(RPC_URL))


def post_discord(msg):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={
            "username": "Riptide Gas Monitor",
            "content": msg[:2000],
        }, timeout=10)
    except Exception:
        pass


def check_gas():
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        balance_wei = w3.eth.get_balance(Web3.to_checksum_address(OPERATOR_ADDR))
        balance_eth = float(w3.from_wei(balance_wei, "ether"))
    except Exception as e:
        print("[GAS " + ts + "] RPC error: " + str(e))
        return

    print("[GAS " + ts + "] Operator balance: " + str(round(balance_eth, 6)) + " ETH")

    # Critical — pause on-chain
    if balance_eth < PAUSE_THRESHOLD:
        if not PAUSE_FILE.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            PAUSE_FILE.write_text(json.dumps({
                "paused_at": datetime.now().isoformat(),
                "balance": balance_eth,
                "reason": "Gas below " + str(PAUSE_THRESHOLD) + " ETH",
            }))
            print("[GAS " + ts + "] PAUSED on-chain submissions")
            post_discord(
                "**[RIPTIDE GAS CRITICAL]** Operator balance: "
                + str(round(balance_eth, 4)) + " ETH\n"
                "On-chain submissions PAUSED. Kraken trading continues.\n"
                "Mine more SepETH at https://sepolia-faucet.pk910.de/\n"
                "Address: `" + OPERATOR_ADDR + "`"
            )
        return

    # Low — warn but keep going
    if balance_eth < WARN_THRESHOLD:
        post_discord(
            "**[RIPTIDE GAS WARNING]** Operator balance: "
            + str(round(balance_eth, 4)) + " ETH\n"
            "Running low on Sepolia gas. Mine more soon.\n"
            "Address: `" + OPERATOR_ADDR + "`\n"
            "Faucet: https://sepolia-faucet.pk910.de/"
        )
        print("[GAS " + ts + "] WARNING: low gas")
        return

    # Healthy — unpause if previously paused
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("[GAS " + ts + "] RESUMED on-chain submissions")
        post_discord(
            "**[RIPTIDE GAS OK]** Operator balance: "
            + str(round(balance_eth, 4)) + " ETH\n"
            "On-chain submissions resumed."
        )


if __name__ == "__main__":
    print("[GAS MONITOR] Checking every " + str(CHECK_INTERVAL) + "s")
    print("[GAS MONITOR] Warn at " + str(WARN_THRESHOLD) + " ETH, pause at " + str(PAUSE_THRESHOLD) + " ETH")
    print("[GAS MONITOR] Operator: " + OPERATOR_ADDR)

    while True:
        check_gas()
        time.sleep(CHECK_INTERVAL)
