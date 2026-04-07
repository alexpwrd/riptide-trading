"""Tests for ClawdTrader agent and monitor."""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# ─── clawdtrader.py tests ─────────────────────────────────────

from clawdtrader import (
    execute_tool, kraken_cmd, KRAKEN_BIN, TOOL_HANDLERS,
    tool_get_portfolio, tool_get_market_overview,
)


class TestKrakenCmd:
    """Test the kraken CLI wrapper."""

    def test_uses_list_not_shell(self):
        """Ensure we're not vulnerable to shell injection."""
        with patch("clawdtrader.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok", stderr="")
            kraken_cmd("paper status -o json")
            args = mock_run.call_args
            # First positional arg should be a list, not a string
            cmd = args[0][0]
            assert isinstance(cmd, list), "kraken_cmd must use list args, not shell=True"
            assert cmd[0] == KRAKEN_BIN
            assert cmd[1:] == ["paper", "status", "-o", "json"]

    def test_timeout_returns_json_error(self):
        """Timeout should return parseable JSON error, not crash."""
        import subprocess
        with patch("clawdtrader.subprocess.run", side_effect=subprocess.TimeoutExpired("kraken", 30)):
            result = kraken_cmd("paper status -o json")
            parsed = json.loads(result)
            assert "error" in parsed
            assert "timed out" in parsed["error"].lower()

    def test_exception_returns_json_error(self):
        """General exceptions should return parseable JSON."""
        with patch("clawdtrader.subprocess.run", side_effect=OSError("No such file")):
            result = kraken_cmd("paper status -o json")
            parsed = json.loads(result)
            assert "error" in parsed


class TestToolExecution:
    """Test tool routing and execution."""

    def test_unknown_tool_returns_error(self):
        result = json.loads(execute_tool("nonexistent_tool", {}))
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_all_tools_registered(self):
        expected = {"get_ticker", "paper_buy", "paper_sell", "get_portfolio", "get_market_overview", "done"}
        assert set(TOOL_HANDLERS.keys()) == expected

    def test_done_tool_returns_summary(self):
        result = json.loads(execute_tool("done", {"summary": "test summary"}))
        assert result["status"] == "done"
        assert result["summary"] == "test summary"

    def test_paper_buy_calls_correct_command(self):
        with patch("clawdtrader.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"ok": true}', stderr="")
            execute_tool("paper_buy", {"pair": "BTCUSD", "amount": 0.001})
            cmd = mock_run.call_args[0][0]
            assert "paper" in cmd
            assert "buy" in cmd
            assert "BTCUSD" in cmd
            assert "0.001" in cmd

    def test_paper_sell_calls_correct_command(self):
        with patch("clawdtrader.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"ok": true}', stderr="")
            execute_tool("paper_sell", {"pair": "ETHUSD", "amount": 0.5})
            cmd = mock_run.call_args[0][0]
            assert "paper" in cmd
            assert "sell" in cmd
            assert "ETHUSD" in cmd

    def test_get_portfolio_parses_json(self):
        mock_status = '{"current_value": 9900, "starting_balance": 10000}'
        mock_trades = '[{"pair": "BTCUSD", "side": "buy"}]'
        with patch("clawdtrader.kraken_cmd", side_effect=[mock_status, mock_trades]):
            result = json.loads(tool_get_portfolio())
            assert result["status"]["current_value"] == 9900
            assert len(result["trades"]) == 1

    def test_get_portfolio_handles_bad_json(self):
        with patch("clawdtrader.kraken_cmd", side_effect=["not json", "also not json"]):
            result = json.loads(tool_get_portfolio())
            assert "raw" in result["status"]

    def test_get_market_overview_parses_ticker(self):
        ticker_response = json.dumps({
            "XXBTZUSD": {
                "c": ["68000.0", "0.001"],
                "b": ["67999.0", "1"],
                "a": ["68001.0", "1"],
                "h": ["69000.0", "69000.0"],
                "l": ["67000.0", "67000.0"],
                "v": ["100.0", "2000.0"],
            }
        })
        with patch("clawdtrader.kraken_cmd", return_value=ticker_response):
            result = json.loads(tool_get_market_overview())
            # All 3 pairs get the same mock data
            assert "BTCUSD" in result
            assert result["BTCUSD"]["last"] == "68000.0"


# ─── monitor.py tests ─────────────────────────────────────────

from monitor import check_portfolio_risk, get_market_snapshot, DATA_DIR, MAX_DRAWDOWN_PCT


class TestRiskChecks:
    """Test portfolio risk / circuit breaker logic."""

    def test_healthy_portfolio(self):
        mock_status = json.dumps({
            "starting_balance": 10000,
            "current_value": 9500,
            "unrealized_pnl": -500,
            "total_trades": 5,
            "open_orders": 1,
        })
        with patch("monitor.kraken_cmd", return_value=mock_status):
            risk = check_portfolio_risk()
            assert risk["ok"] is True
            assert risk["drawdown_pct"] == 5.0

    def test_circuit_breaker_triggers(self):
        mock_status = json.dumps({
            "starting_balance": 10000,
            "current_value": 8000,
            "unrealized_pnl": -2000,
            "total_trades": 10,
            "open_orders": 0,
        })
        with patch("monitor.kraken_cmd", return_value=mock_status):
            risk = check_portfolio_risk()
            assert risk["ok"] is False
            assert "CIRCUIT BREAKER" in risk["reason"]

    def test_circuit_breaker_boundary(self):
        """Exactly at 15% should trigger."""
        mock_status = json.dumps({
            "starting_balance": 10000,
            "current_value": 8500,
            "unrealized_pnl": -1500,
            "total_trades": 8,
            "open_orders": 0,
        })
        with patch("monitor.kraken_cmd", return_value=mock_status):
            risk = check_portfolio_risk()
            assert risk["ok"] is False

    def test_handles_kraken_failure(self):
        with patch("monitor.kraken_cmd", return_value="connection refused"):
            risk = check_portfolio_risk()
            assert "error" in risk
            assert risk["ok"] is True  # fail-open: don't block on transient errors


class TestMarketSnapshot:
    """Test market data parsing."""

    def test_parses_all_pairs(self):
        ticker = json.dumps({
            "XXBTZUSD": {
                "c": ["68000.0"], "b": ["67999.0"], "a": ["68001.0"],
                "h": ["69000.0", "69000.0"], "l": ["67000.0", "67000.0"],
                "v": ["100.0", "2000.0"], "o": "67500.0",
            }
        })
        with patch("monitor.kraken_cmd", return_value=ticker):
            snap = get_market_snapshot()
            for pair in ["BTCUSD", "ETHUSD", "SOLUSD"]:
                assert pair in snap
                assert snap[pair]["last"] == 68000.0
                assert "change_24h_pct" in snap[pair]
                assert "range_position_pct" in snap[pair]

    def test_handles_api_error(self):
        with patch("monitor.kraken_cmd", return_value="error: connection refused"):
            snap = get_market_snapshot()
            for pair in ["BTCUSD", "ETHUSD", "SOLUSD"]:
                assert "error" in snap[pair]


class TestDataDir:
    """Test data directory configuration."""

    def test_default_resolves_to_project_data(self):
        assert str(DATA_DIR).endswith("/data")
        assert "/app/" not in str(DATA_DIR)
