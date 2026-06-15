import time

from lib import ExchangeProxy

# --- Strategy config --------------------------------------------------------
COIN = "BTC"
INTERVAL = "15m"        # candle size used for indicators
FAST = 10               # fast SMA period
SLOW = 30               # slow SMA period
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70     # don't open new longs above this
RSI_OVERSOLD = 30       # don't open new shorts below this
LEVERAGE = 5            # must be <= the harness cap for COIN
NOTIONAL_USD = 50       # position size in USD
TICK_SECONDS = 30       # pause between ticks

_INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


# --- Indicators (plain Python, no extra deps) -------------------------------
def sma(values, n):
    return sum(values[-n:]) / n


def rsi(closes, n):
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-n:]
    gains = sum(d for d in deltas if d > 0) / n
    losses = sum(-d for d in deltas if d < 0) / n
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


class Greevil:
    def __init__(self, exchange_proxy: ExchangeProxy):
        self.exchange_proxy = exchange_proxy
        self._leverage_set = False
        self._sz_decimals = {
            a["name"]: a["szDecimals"]
            for a in exchange_proxy.info.meta()["universe"]
        }

    # --- Helpers ------------------------------------------------------------
    def _closes(self):
        """Recent candle closes, oldest -> newest."""
        info = self.exchange_proxy.info
        bars = SLOW + RSI_PERIOD + 5
        end = int(time.time() * 1000)
        start = end - _INTERVAL_MS[INTERVAL] * bars
        candles = info.candles_snapshot(COIN, INTERVAL, start, end)
        return [float(c["c"]) for c in candles]

    def _signal(self, closes):
        """Return desired position: +1 long, -1 short, 0 flat."""
        fast, slow, momentum = sma(closes, FAST), sma(closes, SLOW), rsi(closes, RSI_PERIOD)
        if fast > slow and momentum < RSI_OVERBOUGHT:
            return 1
        if fast < slow and momentum > RSI_OVERSOLD:
            return -1
        return 0

    def _current_szi(self):
        """Signed position size on COIN (0.0 if flat)."""
        state = self.exchange_proxy.info.user_state(self.exchange_proxy.account_address)
        for p in state["assetPositions"]:
            if p["position"]["coin"] == COIN:
                return float(p["position"]["szi"])
        return 0.0

    def _order_size(self):
        mid = float(self.exchange_proxy.info.all_mids()[COIN])
        return round(NOTIONAL_USD / mid, self._sz_decimals[COIN])

    # --- Main loop ----------------------------------------------------------
    def on_tick(self):
        proxy = self.exchange_proxy
        time.sleep(TICK_SECONDS)

        # Set leverage once (the harness caps it at the configured max).
        if not self._leverage_set:
            ok, _, err = proxy.call(
                "update_leverage", {"leverage": LEVERAGE, "name": COIN, "is_cross": True}
            )
            if not ok:
                print(f"[greevil] could not set leverage: {err}")
                return
            self._leverage_set = True

        closes = self._closes()
        if len(closes) < SLOW + 1:
            print("[greevil] not enough candles yet")
            return

        fast, slow, momentum = sma(closes, FAST), sma(closes, SLOW), rsi(closes, RSI_PERIOD)
        target = self._signal(closes)              # -1 / 0 / +1
        current = self._current_szi()
        current_dir = (current > 0) - (current < 0)  # sign of current position

        name = {1: "LONG", -1: "SHORT", 0: "FLAT"}
        print(
            f"[greevil] {COIN} px={closes[-1]:.1f} "
            f"fast={fast:.1f} slow={slow:.1f} rsi={momentum:.1f} | "
            f"pos={name[current_dir]}({current:g}) signal={name[target]}"
        )

        if target == current_dir:
            return  # already positioned the way the indicators want

        # Close any existing position before flipping/flattening.
        if current_dir != 0:
            ok, _, err = proxy.call("market_close", {"coin": COIN})
            print(f"[greevil] close {COIN}: {'ok' if ok else err}")

        # Open in the new direction (target == 0 means stay flat).
        if target != 0:
            sz = self._order_size()
            ok, result, err = proxy.call(
                "market_open", {"name": COIN, "is_buy": target > 0, "sz": sz}
            )
            side = "LONG" if target > 0 else "SHORT"
            print(f"[greevil] {side} {sz} {COIN}: {'ok' if ok else err}")
