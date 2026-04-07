"""
ERC-8004 On-Chain Integration for Riptide
Sepolia Testnet -- Agent Identity, Trade Intents, Validation Checkpoints

Usage:
  python3 agent/erc8004.py register     # Register agent on AgentRegistry
  python3 agent/erc8004.py claim        # Claim 0.05 ETH from HackathonVault
  python3 agent/erc8004.py status       # Check registration + reputation
  python3 agent/erc8004.py test-trade   # Submit a test TradeIntent

As a library:
  from erc8004 import submit_trade_intent, post_checkpoint
"""

import json
import os
import sys
import time
import warnings
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore', message='.*MismatchedABI.*')

from web3 import Web3
from eth_account import Account

# --- Config ---
DATA_DIR = Path(os.environ.get("CLAWDTRADER_DATA",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))


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
CHAIN_ID = int(os.environ.get("SEPOLIA_CHAIN_ID", "11155111"))

OPERATOR_KEY = os.environ.get("OPERATOR_PRIVATE_KEY", "")
AGENT_WALLET_KEY = os.environ.get("AGENT_WALLET_PRIVATE_KEY", "")
OPERATOR_ADDR = os.environ.get("OPERATOR_ADDRESS", "")
AGENT_WALLET_ADDR = os.environ.get("AGENT_WALLET_ADDRESS", "")

AGENT_REGISTRY = os.environ.get("AGENT_REGISTRY_ADDRESS", "0x97b07dDc405B0c28B17559aFFE63BdB3632d0ca3")
HACKATHON_VAULT = os.environ.get("HACKATHON_VAULT_ADDRESS", "0x0E7CD8ef9743FEcf94f9103033a044caBD45fC90")
RISK_ROUTER = os.environ.get("RISK_ROUTER_ADDRESS", "0xd6A6952545FF6E6E6681c2d15C59f9EB8F40FdBC")
REPUTATION_REGISTRY = os.environ.get("REPUTATION_REGISTRY_ADDRESS", "0x423a9904e39537a9997fbaF0f220d79D7d545763")
VALIDATION_REGISTRY = os.environ.get("VALIDATION_REGISTRY_ADDRESS", "0x92bF63E5C7Ac6980f237a7164Ab413BE226187F1")

AGENT_ID = os.environ.get("AGENT_ID", "")

# --- ABIs (JSON format for web3.py) ---
AGENT_REGISTRY_ABI = [
    {"type": "function", "name": "register", "inputs": [{"name": "agentWallet", "type": "address"}, {"name": "name", "type": "string"}, {"name": "description", "type": "string"}, {"name": "capabilities", "type": "string[]"}, {"name": "agentURI", "type": "string"}], "outputs": [{"name": "agentId", "type": "uint256"}], "stateMutability": "nonpayable"},
    {"type": "function", "name": "isRegistered", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view"},
    {"type": "function", "name": "getAgent", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "tuple", "components": [{"name": "operatorWallet", "type": "address"}, {"name": "agentWallet", "type": "address"}, {"name": "name", "type": "string"}, {"name": "description", "type": "string"}, {"name": "capabilities", "type": "string[]"}, {"name": "registeredAt", "type": "uint256"}, {"name": "active", "type": "bool"}]}], "stateMutability": "view"},
    {"type": "function", "name": "getSigningNonce", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"type": "event", "name": "AgentRegistered", "anonymous": False, "inputs": [{"name": "agentId", "type": "uint256", "indexed": True}, {"name": "operatorWallet", "type": "address", "indexed": True}, {"name": "agentWallet", "type": "address", "indexed": True}, {"name": "name", "type": "string", "indexed": False}]},
]

HACKATHON_VAULT_ABI = [
    {"type": "function", "name": "claimAllocation", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [], "stateMutability": "nonpayable"},
    {"type": "function", "name": "getBalance", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"type": "function", "name": "hasClaimed", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view"},
    {"type": "function", "name": "allocationPerTeam", "inputs": [], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

RISK_ROUTER_ABI = [
    {"type": "function", "name": "submitTradeIntent", "inputs": [{"name": "intent", "type": "tuple", "components": [{"name": "agentId", "type": "uint256"}, {"name": "agentWallet", "type": "address"}, {"name": "pair", "type": "string"}, {"name": "action", "type": "string"}, {"name": "amountUsdScaled", "type": "uint256"}, {"name": "maxSlippageBps", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"}]}, {"name": "signature", "type": "bytes"}], "outputs": [{"name": "approved", "type": "bool"}, {"name": "reason", "type": "string"}], "stateMutability": "nonpayable"},
    {"type": "function", "name": "simulateIntent", "inputs": [{"name": "intent", "type": "tuple", "components": [{"name": "agentId", "type": "uint256"}, {"name": "agentWallet", "type": "address"}, {"name": "pair", "type": "string"}, {"name": "action", "type": "string"}, {"name": "amountUsdScaled", "type": "uint256"}, {"name": "maxSlippageBps", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"}]}], "outputs": [{"name": "valid", "type": "bool"}, {"name": "reason", "type": "string"}], "stateMutability": "view"},
    {"type": "function", "name": "getIntentNonce", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"type": "event", "name": "TradeIntentSubmitted", "anonymous": False, "inputs": [{"name": "agentId", "type": "uint256", "indexed": True}, {"name": "intentHash", "type": "bytes32", "indexed": True}, {"name": "pair", "type": "string", "indexed": False}, {"name": "action", "type": "string", "indexed": False}, {"name": "amountUsdScaled", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "TradeApproved", "anonymous": False, "inputs": [{"name": "agentId", "type": "uint256", "indexed": True}, {"name": "intentHash", "type": "bytes32", "indexed": True}, {"name": "amountUsdScaled", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "TradeRejected", "anonymous": False, "inputs": [{"name": "agentId", "type": "uint256", "indexed": True}, {"name": "intentHash", "type": "bytes32", "indexed": True}, {"name": "reason", "type": "string", "indexed": False}]},
]

VALIDATION_REGISTRY_ABI = [
    {"type": "function", "name": "postAttestation", "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "checkpointHash", "type": "bytes32"}, {"name": "score", "type": "uint8"}, {"name": "proofType", "type": "uint8"}, {"name": "proof", "type": "bytes"}, {"name": "notes", "type": "string"}], "outputs": [], "stateMutability": "nonpayable"},
    {"type": "function", "name": "getAverageValidationScore", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

REPUTATION_REGISTRY_ABI = [
    {"type": "function", "name": "submitFeedback", "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "score", "type": "uint8"}, {"name": "outcomeRef", "type": "bytes32"}, {"name": "comment", "type": "string"}, {"name": "feedbackType", "type": "uint8"}], "outputs": [], "stateMutability": "nonpayable"},
    {"type": "function", "name": "getAverageScore", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

# --- EIP-712 Types ---
RISK_ROUTER_DOMAIN = {
    "name": "RiskRouter",
    "version": "1",
    "chainId": CHAIN_ID,
    "verifyingContract": RISK_ROUTER,
}

TRADE_INTENT_TYPES = {
    "TradeIntent": [
        {"name": "agentId", "type": "uint256"},
        {"name": "agentWallet", "type": "address"},
        {"name": "pair", "type": "string"},
        {"name": "action", "type": "string"},
        {"name": "amountUsdScaled", "type": "uint256"},
        {"name": "maxSlippageBps", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]
}

# --- Web3 Setup ---
import threading as _threading
_tx_lock = _threading.Lock()

w3 = Web3(Web3.HTTPProvider(RPC_URL))


def get_contracts():
    registry = w3.eth.contract(address=Web3.to_checksum_address(AGENT_REGISTRY), abi=AGENT_REGISTRY_ABI)
    vault = w3.eth.contract(address=Web3.to_checksum_address(HACKATHON_VAULT), abi=HACKATHON_VAULT_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(RISK_ROUTER), abi=RISK_ROUTER_ABI)
    validation = w3.eth.contract(address=Web3.to_checksum_address(VALIDATION_REGISTRY), abi=VALIDATION_REGISTRY_ABI)
    reputation = w3.eth.contract(address=Web3.to_checksum_address(REPUTATION_REGISTRY), abi=REPUTATION_REGISTRY_ABI)
    return registry, vault, router, validation, reputation


def send_tx(tx, private_key):
    with _tx_lock:
        # Re-read nonce under lock to avoid collisions from concurrent threads
        tx["nonce"] = w3.eth.get_transaction_count(Web3.to_checksum_address(OPERATOR_ADDR))
        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print("  TX sent: " + tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print("  TX mined in block " + str(receipt.blockNumber) + ", gas used: " + str(receipt.gasUsed))
    if receipt.status != 1:
        print("  TX REVERTED!")
    return receipt


# --- Registration ---
def register_agent():
    registry, _, _, _, _ = get_contracts()
    operator = Web3.to_checksum_address(OPERATOR_ADDR)
    agent_wallet = Web3.to_checksum_address(AGENT_WALLET_ADDR)

    print("Registering Riptide on AgentRegistry...")
    print("  Operator:     " + operator)
    print("  Agent Wallet: " + agent_wallet)
    print("  Registry:     " + AGENT_REGISTRY)

    metadata = {
        "name": "Riptide",
        "description": "Multi-agent autonomous crypto trading system with sentinel risk management, LLM-driven strategy, and real-time Kraken CLI execution",
        "capabilities": ["trading", "eip712-signing", "risk-management", "multi-model-orchestration"],
        "version": "3.0",
        "models": ["nemotron-cascade-2", "phi4-mini"],
        "exchange": "Kraken",
        "pairs": ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "ALGOUSD", "ADAUSD"],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "agent-metadata.json").write_text(json.dumps(metadata, indent=2))

    tx = registry.functions.register(
        agent_wallet,
        "Riptide",
        "Multi-agent autonomous crypto trading system -- sentinel + LLM trader + Kraken CLI",
        ["trading", "eip712-signing", "risk-management", "multi-model-orchestration"],
        "file://agent-metadata.json",
    ).build_transaction({
        "from": operator,
        "nonce": w3.eth.get_transaction_count(operator),
        "gas": 500000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })

    receipt = send_tx(tx, OPERATOR_KEY)
    logs = registry.events.AgentRegistered().process_receipt(receipt)
    if logs:
        agent_id = logs[0]["args"]["agentId"]
        print("\n  Agent registered! agentId = " + str(agent_id))

        (DATA_DIR / "agent-id.json").write_text(json.dumps({
            "agentId": agent_id, "operator": operator, "agentWallet": agent_wallet,
        }))

        env_path = Path(__file__).parent.parent / ".env"
        content = env_path.read_text()
        content = content.replace("AGENT_ID=", "AGENT_ID=" + str(agent_id), 1)
        env_path.write_text(content)
        print("  Updated .env with AGENT_ID=" + str(agent_id))
        return agent_id
    else:
        print("  No AgentRegistered event found in receipt")
        return None


# --- Vault Claim ---
def claim_vault():
    agent_id = int(AGENT_ID) if AGENT_ID else None
    if not agent_id:
        print("ERROR: AGENT_ID not set. Run 'register' first.")
        return

    _, vault, _, _, _ = get_contracts()
    operator = Web3.to_checksum_address(OPERATOR_ADDR)

    claimed = vault.functions.hasClaimed(agent_id).call()
    if claimed:
        balance = vault.functions.getBalance(agent_id).call()
        print("Already claimed. Vault balance: " + str(w3.from_wei(balance, 'ether')) + " ETH")
        return

    print("Claiming sandbox capital for agentId=" + str(agent_id) + "...")
    tx = vault.functions.claimAllocation(agent_id).build_transaction({
        "from": operator,
        "nonce": w3.eth.get_transaction_count(operator),
        "gas": 150000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })
    receipt = send_tx(tx, OPERATOR_KEY)
    balance = vault.functions.getBalance(agent_id).call()
    print("  Claimed! Vault balance: " + str(w3.from_wei(balance, 'ether')) + " ETH")


# --- Trade Intent Submission ---
def submit_trade_intent(pair, action, amount_usd, max_slippage_bps=100):
    agent_id = int(AGENT_ID) if AGENT_ID else None
    if not agent_id:
        print("ERROR: AGENT_ID not set")
        return None

    _, _, router, _, _ = get_contracts()
    agent_wallet = Web3.to_checksum_address(AGENT_WALLET_ADDR)

    nonce = router.functions.getIntentNonce(agent_id).call()
    deadline = int(time.time()) + 300
    amount_scaled = int(amount_usd * 100)

    intent = {
        "agentId": agent_id,
        "agentWallet": agent_wallet,
        "pair": pair,
        "action": action,
        "amountUsdScaled": amount_scaled,
        "maxSlippageBps": max_slippage_bps,
        "nonce": nonce,
        "deadline": deadline,
    }

    print("  [ERC-8004] Submitting TradeIntent: " + action + " " + pair + " $" + str(amount_usd) + " (nonce=" + str(nonce) + ")")

    full_message = {
        "types": TRADE_INTENT_TYPES,
        "domain": RISK_ROUTER_DOMAIN,
        "primaryType": "TradeIntent",
        "message": intent,
    }
    signed = Account.sign_typed_data(AGENT_WALLET_KEY, full_message=full_message)

    intent_tuple = (
        intent["agentId"], intent["agentWallet"], intent["pair"], intent["action"],
        intent["amountUsdScaled"], intent["maxSlippageBps"], intent["nonce"], intent["deadline"],
    )

    operator = Web3.to_checksum_address(OPERATOR_ADDR)
    tx = router.functions.submitTradeIntent(intent_tuple, signed.signature).build_transaction({
        "from": operator,
        "nonce": w3.eth.get_transaction_count(operator),
        "gas": 300000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })

    try:
        receipt = send_tx(tx, OPERATOR_KEY)
        approved = router.events.TradeApproved().process_receipt(receipt)
        rejected = router.events.TradeRejected().process_receipt(receipt)
        if approved:
            print("  TradeApproved: " + pair + " " + action + " $" + str(amount_usd))
        elif rejected:
            reason = rejected[0]["args"]["reason"] if rejected else "unknown"
            print("  TradeRejected: " + reason)
        log_checkpoint(agent_id, pair, action, amount_usd, "trade_intent", receipt.transactionHash.hex())
        return receipt
    except Exception as e:
        print("  TradeIntent failed: " + str(e))
        return None


# --- Validation Checkpoint ---
def post_checkpoint(reasoning, action="HOLD", pair="", amount_usd=0, price_usd=0, score=80):
    agent_id = int(AGENT_ID) if AGENT_ID else None
    if not agent_id:
        print("ERROR: AGENT_ID not set")
        return None

    _, _, _, validation, _ = get_contracts()
    operator = Web3.to_checksum_address(OPERATOR_ADDR)

    checkpoint_data = json.dumps({
        "agentId": agent_id,
        "timestamp": int(time.time()),
        "action": action,
        "pair": pair,
        "amountUsdScaled": int(amount_usd * 100),
        "priceUsdScaled": int(price_usd * 100),
        "reasoningHash": Web3.keccak(text=reasoning).hex(),
    }, sort_keys=True)
    checkpoint_hash = Web3.keccak(text=checkpoint_data)

    if pair:
        notes = action + " " + pair + " $" + str(int(amount_usd)) + " @ $" + str(price_usd)
    else:
        notes = reasoning[:100]

    print("  [ERC-8004] Posting checkpoint: " + notes[:60] + "...")

    tx = validation.functions.postAttestation(
        agent_id, checkpoint_hash, min(score, 100), 0, b"", notes[:256],
    ).build_transaction({
        "from": operator,
        "nonce": w3.eth.get_transaction_count(operator),
        "gas": 500000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })

    try:
        receipt = send_tx(tx, OPERATOR_KEY)
        print("  Checkpoint posted (score=" + str(score) + ")")
        log_checkpoint(agent_id, pair, action, amount_usd, "validation", receipt.transactionHash.hex(), reasoning)
        return receipt
    except Exception as e:
        print("  Checkpoint failed: " + str(e))
        return None


# --- Local Audit Log ---
def log_checkpoint(agent_id, pair, action, amount_usd, checkpoint_type, tx_hash, reasoning=""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "agentId": agent_id,
        "type": checkpoint_type,
        "pair": pair,
        "action": action,
        "amount_usd": amount_usd,
        "tx_hash": tx_hash,
        "reasoning": reasoning[:500] if reasoning else "",
    }
    with open(DATA_DIR / "checkpoints.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Status Check ---
def check_status():
    registry, vault, router, validation, reputation = get_contracts()

    print("\n=== Riptide ERC-8004 Status ===")
    print("Network:        Sepolia (Chain " + str(CHAIN_ID) + ")")
    print("Operator:       " + OPERATOR_ADDR)
    print("Agent Wallet:   " + AGENT_WALLET_ADDR)

    op_balance = w3.eth.get_balance(Web3.to_checksum_address(OPERATOR_ADDR))
    print("Operator ETH:   " + str(w3.from_wei(op_balance, 'ether')) + " ETH")

    agent_id = int(AGENT_ID) if AGENT_ID else None
    if agent_id:
        print("Agent ID:       " + str(agent_id))
        try:
            is_reg = registry.functions.isRegistered(agent_id).call()
            print("Registered:     " + ("Yes" if is_reg else "No"))
        except Exception as e:
            print("Registered:     Error: " + str(e))
        try:
            claimed = vault.functions.hasClaimed(agent_id).call()
            balance = vault.functions.getBalance(agent_id).call()
            print("Vault claimed:  " + ("Yes" if claimed else "No") + " (" + str(w3.from_wei(balance, 'ether')) + " ETH)")
        except Exception as e:
            print("Vault claimed:  Error: " + str(e))
        try:
            nonce = router.functions.getIntentNonce(agent_id).call()
            print("Trade nonce:    " + str(nonce) + " (= trades submitted)")
        except Exception as e:
            print("Trade nonce:    Error: " + str(e))
        try:
            val_score = validation.functions.getAverageValidationScore(agent_id).call()
            print("Validation:     " + str(val_score) + "/100")
        except Exception as e:
            print("Validation:     Error: " + str(e))
        try:
            rep_score = reputation.functions.getAverageScore(agent_id).call()
            print("Reputation:     " + str(rep_score) + "/100")
        except Exception as e:
            print("Reputation:     Error: " + str(e))
    else:
        print("Agent ID:       Not registered yet")
        print("\n  Fund the operator wallet with Sepolia ETH, then run:")
        print("  python3 agent/erc8004.py register")

    print()


# --- CLI ---
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "register":
        if not w3.is_connected():
            print("ERROR: Cannot connect to Sepolia RPC")
            sys.exit(1)
        balance = w3.eth.get_balance(Web3.to_checksum_address(OPERATOR_ADDR))
        if balance == 0:
            print("\n  Operator wallet has 0 ETH!")
            print("  Fund this address with Sepolia ETH:")
            print("\n  " + OPERATOR_ADDR)
            print("\n  Faucets:")
            print("    https://cloud.google.com/application/web3/faucet/ethereum/sepolia")
            print("    https://www.alchemy.com/faucets/ethereum-sepolia")
            print("    https://sepolia-faucet.pk910.de/")
            sys.exit(1)
        register_agent()

    elif cmd == "claim":
        claim_vault()

    elif cmd == "status":
        check_status()

    elif cmd == "test-trade":
        submit_trade_intent("BTCUSD", "BUY", 100)

    elif cmd == "test-checkpoint":
        post_checkpoint(
            reasoning="Market analysis shows BTC momentum positive, volume increasing. Placing buy order.",
            action="BUY", pair="BTCUSD", amount_usd=100, price_usd=69000, score=85,
        )

    else:
        print("Unknown command: " + cmd)
        print("Commands: register, claim, status, test-trade, test-checkpoint")
