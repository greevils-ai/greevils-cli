"""Example greevils agent -- the bundle's required entrypoint is THIS file, `entry.py`.

Your agent is a normal program that runs in its own sandboxed process with NO trading key. It
trades by making plain HTTP calls to the harness's loopback API -- there is NO SDK to import.
The harness URL is in the env var GREEVILS_AGENT_URL (e.g. http://127.0.0.1:8081); use any HTTP
client / language you like. Market data needs no key -- read it from Hyperliquid directly.
Structure your code however you want across files (this example splits indicators out); declare
pip deps in `requirements.txt` (installed inside the TEE before this runs). The TEE runs
`python entry.py`.

The trading API (all JSON):
  POST /call     {"method": "<exchange method>", "params": {...}}  -> {"ok", "result", "error"}
  GET  /state    -> {"account_value", "withdrawable", "positions"}
  GET  /address  -> {"address"}

The harness whitelists which Exchange methods you may call (a security boundary), but it does NOT
enforce trading policy -- which coins or how much leverage. That is checked by the validator, so an
out-of-policy trade is accepted here yet penalized downstream. Keep your agent within policy.
"""
import json
import os
import time
import urllib.error
import urllib.request

from hyperliquid.info import Info
from hyperliquid.utils import constants

from indicators import rsi, sma

# --- Trading client: ~12 lines of stdlib, no dependencies. Copy/replace with your own. --------
_URL = os.environ["GREEVILS_AGENT_URL"]  # the harness's loopback trading API


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:        # 4xx/409 still carry a JSON {ok, error} body
        return json.loads(e.read() or b"{}")


def call(method, **params):
    j = _req("POST", "/call", {"method": method, "params": params})
    return j.get("ok", False), j.get("result"), j.get("error", "")


def state():
    return _req("GET", "/state")


def address():
    return _req("GET", "/address").get("address")
# ----------------------------------------------------------------------------------------------

COIN = "BTC"
INTERVAL = "15m"
FAST, SLOW, RSI_PERIOD = 10, 30, 14
RSI_OVERBOUGHT, RSI_OVERSOLD = 70, 30
LEVERAGE = 5
NOTIONAL_USD = 50
TICK_SECONDS = 30
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def recent_closes(info):
    bars = SLOW + RSI_PERIOD + 5
    end = int(time.time() * 1000)
    start = end - _INTERVAL_MS[INTERVAL] * bars
    return [float(c["c"]) for c in info.candles_snapshot(COIN, INTERVAL, start, end)]


def current_szi():
    """Signed position size on COIN (0.0 if flat), read from the harness."""
    for p in state().get("positions", []):
        if p.get("position", {}).get("coin") == COIN:
            return float(p["position"]["szi"])
    return 0.0


def main():
    print("agent account:", address(), flush=True)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    call("update_leverage", leverage=LEVERAGE, name=COIN, is_cross=True)

    while True:
        time.sleep(TICK_SECONDS)
        try:
            closes = recent_closes(info)
            if len(closes) < SLOW + 1:
                continue
            fast, slow, momentum = sma(closes, FAST), sma(closes, SLOW), rsi(closes, RSI_PERIOD)
            if fast > slow and momentum < RSI_OVERBOUGHT:
                target = 1
            elif fast < slow and momentum > RSI_OVERSOLD:
                target = -1
            else:
                target = 0

            current = current_szi()
            current_dir = (current > 0) - (current < 0)
            print(f"{COIN} px={closes[-1]:.1f} fast={fast:.1f} slow={slow:.1f} "
                  f"rsi={momentum:.1f} pos={current:g} target={target}", flush=True)
            if target == current_dir:
                continue

            if current_dir != 0:
                ok, _, err = call("market_close", coin=COIN)
                print(f"close {COIN}: {'ok' if ok else err}", flush=True)
            if target != 0:
                mid = float(info.all_mids()[COIN])
                sz = round(NOTIONAL_USD / mid, 5)
                ok, _, err = call("market_open", name=COIN, is_buy=target > 0, sz=sz)
                print(f"{'long' if target > 0 else 'short'} {sz} {COIN}: {'ok' if ok else err}",
                      flush=True)
        except Exception as e:  # one bad tick shouldn't kill the agent
            print(f"tick error: {e}", flush=True)


if __name__ == "__main__":
    main()
