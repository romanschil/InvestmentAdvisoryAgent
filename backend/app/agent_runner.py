"""
Helper functions that wrap the existing investment_advisory_agent.py logic
and provide file-locking and subprocess execution.
"""
import os
import json
import subprocess
import time
from typing import Optional

import portalocker

# import the agent functions we need
import investment_advisory_agent as agent

LOCK_PATH = agent.STATE_FILE + ".lock"
STATE_PATH = agent.STATE_FILE
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOCK_TIMEOUT = 5


def _acquire_lock():
    # create lockfile and acquire
    lock_file = open(LOCK_PATH, "w")
    portalocker.lock(lock_file, portalocker.LockFlags.EXCLUSIVE)
    return lock_file


def _release_lock(lock_file):
    try:
        portalocker.unlock(lock_file)
    except Exception:
        pass
    try:
        lock_file.close()
    except Exception:
        pass


def read_state_locked() -> dict:
    lock = _acquire_lock()
    try:
        return agent.load_state()
    finally:
        _release_lock(lock)


def write_state_locked(state: dict):
    lock = _acquire_lock()
    try:
        agent.save_state(state)
    finally:
        _release_lock(lock)


def run_agent_subprocess(timeout: int = 120) -> str:
    """Run the existing agent script in a subprocess and return combined output."""
    cmd = ["python", "investment_advisory_agent.py"]
    env = os.environ.copy()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = "--- TIMEOUT ---"
    return out or ""


def restart_portfolio_wrapper(auto_buy: bool = True) -> dict:
    """Call the restart_portfolio function and (optionally) perform buys.
    The existing restart_portfolio already performs immediate buys when picks available.
    """
    lock = _acquire_lock()
    try:
        state = agent.restart_portfolio()
        # restart_portfolio already saves state
        return state
    finally:
        _release_lock(lock)


def confirm_action_wrapper(action_id: str, action_type: str):
    """Execute a pending action immediately (sell or buy). Returns transaction dict or raises."""
    lock = _acquire_lock()
    try:
        state = agent.load_state()
        action = next((a for a in state.get("pending_actions", []) if a["id"] == action_id and a["type"] == action_type), None)
        if not action:
            raise RuntimeError("Action not found")

        # refresh prices/fx
        universe_tickers = [u["ticker"] for u in agent.CANDIDATE_UNIVERSE]
        held_tickers = [p["ticker"] for p in state.get("positions", [])]
        prices = agent.fetch_prices(universe_tickers + held_tickers)
        fx = agent.fetch_fx_rates()

        if action_type == "sell":
            tx = agent.execute_sell_action(state, action, prices, fx)
        elif action_type == "buy":
            tx = agent.execute_buy_action(state, action, prices, fx)
        else:
            raise RuntimeError("Unknown action type")

        agent.save_state(state)
        return tx
    finally:
        _release_lock(lock)
