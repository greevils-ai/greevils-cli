"""Plain-Python indicators -- a second file, to show the agent is a multi-file bundle now."""


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
