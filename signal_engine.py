"""
BTC Signal Engine — 4H Edition
===============================
- Data source   : Binance public API (no API key required)
- Timeframe     : 4-hour candles
- Cadence       : one full analysis + Telegram message every 4 hours
                  (sent for every candle — always BUY or SELL, never HOLD)
- Position size : 100% of capital per trade
- Risk          : ATR-based dynamic SL (1%-2%) and TP (3%-5%)
                  SL is always strictly smaller than TP
- Goal          : 66 completed trades (a trade = a BUY/SELL direction flip)

Indicators used in the scoring system:
  - RSI (14)
  - MACD (12, 26, 9)
  - Bollinger Bands (20, 2)
  - EMA 20 / EMA 50 crossover
  - Support & Resistance from swing highs/lows (last 20 candles)
  - ATR (14) for volatility
  - Volume vs 20-period average
  - ADX (14) for trend strength
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ═══════════════════════════════════════════════════════════
#  TELEGRAM CREDENTIALS
# ═══════════════════════════════════════════════════════════
# Read from environment variables so the secrets never live in the code/repo.
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# ═══════════════════════════════════════════════════════════

# ── Market config ─────────────────────────────────────────
SYMBOL       = "BTCUSDT"
INTERVAL     = "4h"
CANDLES      = 200            # enough history for EMA50, ADX14, etc.

# ── Risk config ───────────────────────────────────────────
SL_MIN_PCT   = 0.01           # 1%
SL_MAX_PCT   = 0.02           # 2%
TP_MIN_PCT   = 0.03           # 3%
TP_MAX_PCT   = 0.05           # 5%

# Volatility band (in terms of ATR as a fraction of price) used to scale
# the dynamic SL/TP linearly between the min/max bounds above.
ATR_LOW_PCT  = 0.010          # <= 1.0% ATR/price -> low volatility  -> tight targets
ATR_HIGH_PCT = 0.030          # >= 3.0% ATR/price -> high volatility -> wide targets

# ── Engine config ─────────────────────────────────────────
GOAL_TRADES  = 66
SLEEP_BETWEEN_CYCLES = 4 * 60 * 60   # 4 hours
ALIGN_TO_CANDLE_CLOSE = True         # sleep until next 4H boundary + buffer
CANDLE_CLOSE_BUFFER_SEC = 30         # wait a few seconds after close to let the candle finalize

# ── Filesystem ────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
LOG_DIR    = BASE_DIR / "logs"
TRADE_LOG  = DATA_DIR / "trades.json"
STATE_FILE = DATA_DIR / "state.json"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "engine.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("signal_engine")


# ══════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════
def fetch_candles() -> list[dict]:
    """Pull the latest 4H OHLCV candles from Binance public API."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": CANDLES}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return [
        {
            "time":  datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat(),
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
            "vol":   float(c[5]),
        }
        for c in r.json()
    ]


# ══════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════
def ema(values: list[float], period: int) -> list[float | None]:
    """Classic EMA. First `period-1` entries are None; entry at index period-1 is the SMA seed."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        prev = out[i - 1]
        out[i] = values[i] * k + prev * (1 - k)  # type: ignore[operator]
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI — final value only."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def macd(closes: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    """
    Return (macd_now, signal_now, macd_prev, signal_prev).
    MACD = EMA12 - EMA26; Signal = EMA9 of MACD.
    """
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd_line: list[float | None] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(e12, e26)
    ]
    valid = [v for v in macd_line if v is not None]
    if len(valid) < 10:
        return None, None, None, None
    sig_valid = ema(valid, 9)
    # Align signal back to macd_line indices.
    first_valid_idx = next(i for i, v in enumerate(macd_line) if v is not None)
    signal_line: list[float | None] = [None] * len(macd_line)
    for j, s in enumerate(sig_valid):
        signal_line[first_valid_idx + j] = s

    macd_now    = macd_line[-1]
    macd_prev   = macd_line[-2] if len(macd_line) >= 2 else None
    signal_now  = signal_line[-1]
    signal_prev = signal_line[-2] if len(signal_line) >= 2 else None
    return macd_now, signal_now, macd_prev, signal_prev


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float]:
    w = closes[-period:]
    mid = sum(w) / period
    var = sum((x - mid) ** 2 for x in w) / period
    std = math.sqrt(var)
    return round(mid + mult * std, 2), round(mid, 2), round(mid - mult * std, 2)


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    trs: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            trs.append(tr)
    return trs


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Wilder's ATR — final value only."""
    trs = true_range(highs, lows, closes)
    if len(trs) < period + 1:
        return sum(trs) / max(len(trs), 1)
    atr_val = sum(trs[1:period + 1]) / period
    for i in range(period + 1, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> tuple[float, float, float]:
    """
    Return (adx, +DI, -DI) — Wilder's classic formulation.
    """
    n = len(closes)
    if n < period * 2 + 1:
        return 0.0, 0.0, 0.0

    plus_dm, minus_dm, trs = [0.0], [0.0], [highs[0] - lows[0]]
    for i in range(1, n):
        up_move    = highs[i] - highs[i - 1]
        down_move  = lows[i - 1] - lows[i]
        plus_dm.append(up_move   if (up_move   > down_move and up_move   > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move   and down_move > 0) else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)

    # Wilder-smooth +DM, -DM, TR.
    def wilder_smooth(series: list[float]) -> list[float]:
        out = [0.0] * len(series)
        out[period] = sum(series[1:period + 1])
        for i in range(period + 1, len(series)):
            out[i] = out[i - 1] - (out[i - 1] / period) + series[i]
        return out

    sm_plus  = wilder_smooth(plus_dm)
    sm_minus = wilder_smooth(minus_dm)
    sm_tr    = wilder_smooth(trs)

    plus_di  = [0.0] * n
    minus_di = [0.0] * n
    dx       = [0.0] * n
    for i in range(period, n):
        if sm_tr[i] == 0:
            continue
        plus_di[i]  = 100 * sm_plus[i]  / sm_tr[i]
        minus_di[i] = 100 * sm_minus[i] / sm_tr[i]
        denom = plus_di[i] + minus_di[i]
        if denom != 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom

    # ADX = Wilder smoothing of DX over `period`.
    if n < period * 2:
        return 0.0, round(plus_di[-1], 2), round(minus_di[-1], 2)
    first_adx_idx = period * 2
    adx_val = sum(dx[period + 1:first_adx_idx + 1]) / period
    for i in range(first_adx_idx + 1, n):
        adx_val = (adx_val * (period - 1) + dx[i]) / period

    return round(adx_val, 2), round(plus_di[-1], 2), round(minus_di[-1], 2)


def swing_levels(candles: list[dict], lookback: int = 20, fractal: int = 2) -> tuple[float, float, list[float], list[float]]:
    """
    Identify swing highs and lows across the last `lookback` candles using a simple
    fractal test (a candle whose high/low is extreme over +/- `fractal` neighbors).
    Returns (nearest_support, nearest_resistance, swing_lows, swing_highs).
    Falls back to window min/max if no fractals are found.
    """
    window = candles[-lookback:]
    highs = [c["high"] for c in window]
    lows  = [c["low"]  for c in window]
    price = candles[-1]["close"]

    swing_highs, swing_lows = [], []
    for i in range(fractal, len(window) - fractal):
        h = highs[i]
        l = lows[i]
        if all(h >= highs[i - k] for k in range(1, fractal + 1)) and \
           all(h >= highs[i + k] for k in range(1, fractal + 1)):
            swing_highs.append(h)
        if all(l <= lows[i - k]  for k in range(1, fractal + 1)) and \
           all(l <= lows[i + k]  for k in range(1, fractal + 1)):
            swing_lows.append(l)

    if not swing_highs:
        swing_highs = [max(highs)]
    if not swing_lows:
        swing_lows = [min(lows)]

    resistances_above = [h for h in swing_highs if h >= price]
    supports_below    = [l for l in swing_lows  if l <= price]

    resistance = min(resistances_above) if resistances_above else max(swing_highs)
    support    = max(supports_below)    if supports_below    else min(swing_lows)
    return round(support, 2), round(resistance, 2), swing_lows, swing_highs


# ══════════════════════════════════════════════════════════
#  SIGNAL SCORING
# ══════════════════════════════════════════════════════════
def dynamic_sl_tp(price: float, atr_value: float, direction: str) -> tuple[float, float, float, float]:
    """
    Scale SL and TP linearly between (SL_MIN..SL_MAX) and (TP_MIN..TP_MAX) based on ATR/price.
    Returns (sl_price, tp_price, sl_pct, tp_pct). SL is always strictly smaller than TP.
    """
    atr_pct = atr_value / price if price else 0.0
    denom = max(ATR_HIGH_PCT - ATR_LOW_PCT, 1e-9)
    vol = (atr_pct - ATR_LOW_PCT) / denom
    vol = max(0.0, min(1.0, vol))

    sl_pct = SL_MIN_PCT + vol * (SL_MAX_PCT - SL_MIN_PCT)
    tp_pct = TP_MIN_PCT + vol * (TP_MAX_PCT - TP_MIN_PCT)

    # Hard guarantee: SL < TP.
    if sl_pct >= tp_pct:
        sl_pct = min(sl_pct, tp_pct - 0.005)

    if direction == "BUY":
        sl_price = price * (1 - sl_pct)
        tp_price = price * (1 + tp_pct)
    else:  # SELL
        sl_price = price * (1 + sl_pct)
        tp_price = price * (1 - tp_pct)

    return round(sl_price, 2), round(tp_price, 2), round(sl_pct * 100, 2), round(tp_pct * 100, 2)


def analyze(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    vols   = [c["vol"]   for c in candles]
    price  = closes[-1]

    rsi_val = rsi(closes, 14)
    macd_now, signal_now, macd_prev, signal_prev = macd(closes)
    bb_up, bb_mid, bb_low = bollinger(closes, 20, 2.0)
    ema20_list = ema(closes, 20)
    ema50_list = ema(closes, 50)
    ema20 = ema20_list[-1]
    ema50 = ema50_list[-1]
    atr_val = atr(highs, lows, closes, 14)
    atr_pct = (atr_val / price * 100) if price else 0.0
    adx_val, plus_di, minus_di = adx(highs, lows, closes, 14)
    support, resistance, _, _ = swing_levels(candles, 20, 2)

    vol_now = vols[-1]
    vol_avg = sum(vols[-20:]) / 20
    vol_ratio = vol_now / vol_avg if vol_avg else 1.0

    # ── Scoring ──
    score = 0
    reasons: list[str] = []

    # RSI
    if rsi_val < 30:
        score += 2; reasons.append(f"RSI deeply oversold ({rsi_val})")
    elif rsi_val < 40:
        score += 1; reasons.append(f"RSI leaning oversold ({rsi_val})")
    elif rsi_val > 70:
        score -= 2; reasons.append(f"RSI deeply overbought ({rsi_val})")
    elif rsi_val > 60:
        score -= 1; reasons.append(f"RSI leaning overbought ({rsi_val})")

    # MACD
    macd_state = "neutral"
    if None not in (macd_now, signal_now, macd_prev, signal_prev):
        if macd_prev < signal_prev and macd_now > signal_now:  # type: ignore[operator]
            score += 2; macd_state = "bullish crossover"; reasons.append("MACD bullish crossover")
        elif macd_prev > signal_prev and macd_now < signal_now:  # type: ignore[operator]
            score -= 2; macd_state = "bearish crossover"; reasons.append("MACD bearish crossover")
        elif macd_now > signal_now:  # type: ignore[operator]
            score += 1; macd_state = "above signal (bullish)"; reasons.append("MACD above signal")
        else:
            score -= 1; macd_state = "below signal (bearish)"; reasons.append("MACD below signal")

    # Bollinger
    if price <= bb_low:
        score += 1; reasons.append("price tagging lower Bollinger band")
    elif price >= bb_up:
        score -= 1; reasons.append("price tagging upper Bollinger band")

    # EMA trend
    trend = "neutral"
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            score += 1; trend = "bullish"; reasons.append("EMA20 > EMA50 (uptrend)")
        elif ema20 < ema50:
            score -= 1; trend = "bearish"; reasons.append("EMA20 < EMA50 (downtrend)")

    # ADX confirmation — only amplifies if a directional edge already exists.
    if adx_val >= 25:
        if plus_di > minus_di:
            score += 1; reasons.append(f"ADX {adx_val} strong trend, +DI>-DI (bull)")
        elif minus_di > plus_di:
            score -= 1; reasons.append(f"ADX {adx_val} strong trend, -DI>+DI (bear)")
    elif adx_val < 20:
        reasons.append(f"ADX {adx_val} weak/range trend")

    # Volume confirmation — rewards the side already favored.
    if vol_ratio >= 1.5:
        if score > 0:
            score += 1; reasons.append(f"volume {vol_ratio:.2f}x avg confirms strength")
        elif score < 0:
            score -= 1; reasons.append(f"volume {vol_ratio:.2f}x avg confirms weakness")
    elif vol_ratio < 0.7:
        reasons.append(f"volume thin at {vol_ratio:.2f}x avg")

    # Support / resistance proximity (within 1% counts as "near")
    near_sup = abs(price - support)    / price < 0.01
    near_res = abs(price - resistance) / price < 0.01
    if near_sup:
        score += 1; reasons.append(f"price near support {support}")
    if near_res:
        score -= 1; reasons.append(f"price near resistance {resistance}")

    # ── Decide action (always BUY or SELL — never HOLD) ──
    # Positive score -> BUY, negative -> SELL. On a dead-even score, break the
    # tie with the fastest momentum reads: MACD histogram, then price vs EMA20,
    # then the direction of the last candle.
    if score > 0:
        action = "BUY"
    elif score < 0:
        action = "SELL"
    else:
        hist = (macd_now - signal_now) if None not in (macd_now, signal_now) else 0.0  # type: ignore[operator]
        if hist != 0:
            action = "BUY" if hist > 0 else "SELL"
            reasons.append(f"tie-break: MACD histogram {'positive' if hist > 0 else 'negative'}")
        elif ema20 is not None and price != ema20:
            action = "BUY" if price > ema20 else "SELL"
            reasons.append(f"tie-break: price {'above' if price > ema20 else 'below'} EMA20")
        else:
            last = candles[-1]
            action = "BUY" if last["close"] >= last["open"] else "SELL"
            reasons.append(f"tie-break: last candle {'green' if action == 'BUY' else 'red'}")

    sl_price, tp_price, sl_pct, tp_pct = dynamic_sl_tp(price, atr_val, action)

    # Confidence out of 10: magnitude of the score, nudged by ADX trend strength.
    # A weak score (e.g. 0-1) still gives a direction but with low confidence.
    adx_boost = 1 if adx_val >= 25 else (-1 if adx_val < 15 else 0)
    confidence = max(1, min(10, min(abs(score), 10) + adx_boost))

    return {
        "action":     action,
        "price":      round(price, 2),
        "tp_price":   tp_price,
        "tp_pct":     tp_pct,
        "sl_price":   sl_price,
        "sl_pct":     sl_pct,
        "support":    support,
        "resistance": resistance,
        "rsi":        rsi_val,
        "macd":       round(macd_now, 2)    if macd_now    is not None else None,
        "signal":     round(signal_now, 2)  if signal_now  is not None else None,
        "macd_state": macd_state,
        "bb_upper":   bb_up,
        "bb_lower":   bb_low,
        "ema20":      round(ema20, 2) if ema20 is not None else None,
        "ema50":      round(ema50, 2) if ema50 is not None else None,
        "trend":      trend,
        "atr":        round(atr_val, 2),
        "atr_pct":    round(atr_pct, 2),
        "adx":        adx_val,
        "plus_di":    plus_di,
        "minus_di":   minus_di,
        "vol_now":    round(vol_now, 2),
        "vol_avg":    round(vol_avg, 2),
        "vol_ratio":  round(vol_ratio, 2),
        "score":      score,
        "confidence": confidence,
        "reasons":    reasons,
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ══════════════════════════════════════════════════════════
#  HUMAN-READABLE ANALYSIS
# ══════════════════════════════════════════════════════════
def written_analysis(sig: dict) -> str:
    trend = sig["trend"]
    rsi_val = sig["rsi"]
    if rsi_val < 30:   momentum = f"momentum is deeply oversold (RSI {rsi_val})"
    elif rsi_val < 45: momentum = f"momentum leans oversold (RSI {rsi_val})"
    elif rsi_val > 70: momentum = f"momentum is deeply overbought (RSI {rsi_val})"
    elif rsi_val > 55: momentum = f"momentum leans overbought (RSI {rsi_val})"
    else:              momentum = f"momentum is neutral (RSI {rsi_val})"

    if   sig["adx"] >= 40: strength = f"a very strong trend (ADX {sig['adx']})"
    elif sig["adx"] >= 25: strength = f"a strong trend (ADX {sig['adx']})"
    elif sig["adx"] >= 20: strength = f"a developing trend (ADX {sig['adx']})"
    else:                  strength = f"a weak/ranging market (ADX {sig['adx']})"

    if   sig["vol_ratio"] >= 1.5: vol_desc = f"volume is elevated at {sig['vol_ratio']}x the 20-period average, confirming participation"
    elif sig["vol_ratio"] >= 1.0: vol_desc = f"volume is in line with the 20-period average ({sig['vol_ratio']}x)"
    elif sig["vol_ratio"] >= 0.7: vol_desc = f"volume is slightly thin at {sig['vol_ratio']}x the 20-period average"
    else:                         vol_desc = f"volume is notably thin at {sig['vol_ratio']}x the 20-period average, signaling hesitation"

    if   sig["atr_pct"] >= 2.5: vola = f"volatility is high (ATR {sig['atr_pct']}% of price), warranting wider targets"
    elif sig["atr_pct"] >= 1.5: vola = f"volatility is moderate (ATR {sig['atr_pct']}% of price)"
    else:                       vola = f"volatility is low (ATR {sig['atr_pct']}% of price), so tighter targets are appropriate"

    levels = (
        f"nearest support sits at ${sig['support']:,.2f} and nearest resistance at "
        f"${sig['resistance']:,.2f}"
    )

    side = "bullish" if sig["action"] == "BUY" else "bearish"
    label = "LONG (BUY)" if sig["action"] == "BUY" else "SHORT (SELL)"
    if abs(sig["score"]) >= 4:
        verdict = f"Taken together, {side} confluence is strong, so the engine is issuing a {label} signal."
    elif abs(sig["score"]) >= 2:
        verdict = f"Taken together, the {side} reads have a moderate edge, so the engine is issuing a {label} signal."
    else:
        verdict = (
            f"The indicators are mixed and only lean slightly {side}, so the engine is issuing a "
            f"{label} signal with low conviction — consider reduced size or a tighter stop."
        )

    return (
        f"BTC is trading at ${sig['price']:,.2f} on the 4H. "
        f"The EMA20/EMA50 relationship shows a {trend} trend, "
        f"and {strength}. In terms of momentum, {momentum}; "
        f"the MACD is currently {sig['macd_state']}. "
        f"On flows, {vol_desc}. "
        f"On the volatility side, {vola}. "
        f"For key levels, {levels}. "
        f"{verdict}"
    )


# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if r.ok:
            log.info("Telegram message sent.")
            return True
        log.error(f"Telegram HTTP {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
    return False


def build_message(sig: dict, trade_num: int | None) -> str:
    if sig["action"] == "BUY":
        emoji, label, tp_dir, sl_dir = "🟢", "LONG (BUY)",  "+", "-"
    else:
        emoji, label, tp_dir, sl_dir = "🔴", "SHORT (SELL)", "-", "+"

    remaining = GOAL_TRADES - (trade_num or 0)
    trade_line = f"📈 *Trade #{trade_num}* of {GOAL_TRADES} — {remaining} remaining"

    analysis = written_analysis(sig)

    return (
        f"{emoji} *BTC/USDT 4H — {label}*\n"
        f"\n"
        f"💰 *Position size:* 100% of capital\n"
        f"📍 *Entry:*        `${sig['price']:,.2f}`\n"
        f"🎯 *Take Profit:*  `${sig['tp_price']:,.2f}`  ({tp_dir}{sig['tp_pct']}%)\n"
        f"🛑 *Stop Loss:*    `${sig['sl_price']:,.2f}`  ({sl_dir}{sig['sl_pct']}%)\n"
        f"\n"
        f"🟩 *Support:*      `${sig['support']:,.2f}`\n"
        f"🟥 *Resistance:*   `${sig['resistance']:,.2f}`\n"
        f"\n"
        f"📊 *Indicators*\n"
        f"  RSI(14):   `{sig['rsi']}`\n"
        f"  MACD:      `{sig['macd']}` | Signal: `{sig['signal']}`  _({sig['macd_state']})_\n"
        f"  EMA20/50:  `{sig['ema20']}` / `{sig['ema50']}`  _({sig['trend']})_\n"
        f"  Bollinger: upper `{sig['bb_upper']}` · lower `{sig['bb_lower']}`\n"
        f"  ATR(14):   `{sig['atr']}`  ({sig['atr_pct']}% of price)\n"
        f"  ADX(14):   `{sig['adx']}`  (+DI `{sig['plus_di']}` · -DI `{sig['minus_di']}`)\n"
        f"  Volume:    `{sig['vol_ratio']}x` vs 20-avg\n"
        f"\n"
        f"🧠 *Analysis*\n{analysis}\n"
        f"\n"
        f"🔥 *Confidence:* {sig['confidence']}/10\n"
        f"{trade_line}\n"
        f"🕐 `{sig['timestamp']}`"
    )


# ══════════════════════════════════════════════════════════
#  PERSISTENCE
# ══════════════════════════════════════════════════════════
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"trade_count": 0, "last_action": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_trade(sig: dict, trade_num: int) -> None:
    trades = []
    if TRADE_LOG.exists():
        try:
            trades = json.loads(TRADE_LOG.read_text())
        except Exception:
            trades = []
    trades.append({"trade": trade_num, **sig})
    TRADE_LOG.write_text(json.dumps(trades, indent=2))


# ══════════════════════════════════════════════════════════
#  SCHEDULING
# ══════════════════════════════════════════════════════════
def seconds_until_next_4h_close(buffer_sec: int = CANDLE_CLOSE_BUFFER_SEC) -> int:
    """Seconds until the next UTC 4H boundary (00, 04, 08, 12, 16, 20) + small buffer."""
    now = datetime.now(timezone.utc)
    hours_to_next = 4 - (now.hour % 4)
    candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hours_to_next)
    while candidate <= now:
        candidate += timedelta(hours=4)
    wait = int((candidate - now).total_seconds()) + buffer_sec
    return max(wait, 1)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def run_cycle(state: dict) -> dict:
    candles = fetch_candles()
    sig = analyze(candles)

    log.info(
        f"BTC=${sig['price']:,.2f} | {sig['action']} | score={sig['score']} | "
        f"RSI={sig['rsi']} | ADX={sig['adx']} | ATR%={sig['atr_pct']} | "
        f"conf={sig['confidence']}/10"
    )

    # Trade counter increments only when the direction flips (BUY -> SELL or
    # SELL -> BUY); a repeated signal means the same position is still on.
    if sig["action"] != state.get("last_action"):
        state["trade_count"] = state.get("trade_count", 0) + 1
        append_trade(sig, state["trade_count"])
        state["last_action"] = sig["action"]
    trade_num = state["trade_count"]

    msg = build_message(sig, trade_num)
    send_telegram(msg)
    save_state(state)

    if state.get("trade_count", 0) >= GOAL_TRADES:
        send_telegram(
            f"🏆 *Goal reached!*\n"
            f"Completed {GOAL_TRADES} trades on the BTC 4H engine. "
            "Engine stopping. Well done!"
        )
        log.info("66-trade goal reached. Engine stopped.")
        state["done"] = True

    return state


def main() -> None:
    log.info("BTC Signal Engine (4H) starting...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables must be set.")
        return
    send_telegram(
        "🤖 *BTC Signal Engine (4H) is live!*\n\n"
        f"📊 Pair: BTCUSDT | Timeframe: 4H\n"
        f"💰 Position: 100% of capital per trade\n"
        f"🎯 TP: 3%-5% (ATR-scaled) | 🛑 SL: 1%-2% (ATR-scaled)\n"
        f"🏁 Goal: {GOAL_TRADES} BUY/SELL trades\n"
        f"🗓️ Cadence: one full analysis every 4 hours (always BUY or SELL)"
    )

    state = load_state()
    while True:
        try:
            state = run_cycle(state)
            if state.get("done"):
                break
        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
            try:
                send_telegram(f"⚠️ Network error — will retry next cycle.\n`{e}`")
            except Exception:
                pass
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            try:
                send_telegram(f"⚠️ Engine error:\n`{e}`")
            except Exception:
                pass

        if ALIGN_TO_CANDLE_CLOSE:
            wait = seconds_until_next_4h_close()
        else:
            wait = SLEEP_BETWEEN_CYCLES
        log.info(f"Sleeping {wait}s until next 4H cycle...")
        time.sleep(wait)


if __name__ == "__main__":
    main()
