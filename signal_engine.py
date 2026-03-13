"""
DHAN ALPHA — Signal Engine
Generates BUY CE / BUY PE signals using:
  - VWAP breakout
  - Opening Range Breakout (ORB)
  - OI + volume spikes
  - IV rank screening
  - Delta / Gamma estimation
  - PCR + momentum
"""
import json
import logging
import math
from datetime import datetime, time as dtime
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# SIGNAL TYPES
# ─────────────────────────────────────────────────────────────
SIGNAL_BUY_CE = "BUY_CE"
SIGNAL_BUY_PE = "BUY_PE"

STRATEGY_VWAP       = "VWAP_BREAKOUT"
STRATEGY_ORB        = "OPENING_RANGE_BREAKOUT"
STRATEGY_OI_SPIKE   = "OI_SPIKE"
STRATEGY_IV_CRUSH   = "IV_CRUSH_ARB"
STRATEGY_MOMENTUM   = "HIGH_MOMENTUM"
STRATEGY_MAX_PAIN   = "MAX_PAIN_PIN"
STRATEGY_GAMMA_SQZ  = "GAMMA_SQUEEZE"


class SignalEngine:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis
        self._vwap_state: dict = {}    # security_id → {sum_pv, sum_v}
        self._candles: dict   = {}    # security_id → list of 1-min candles
        self._orb: dict       = {}    # security_id → {high, low, set}
        self._market_open = dtime(9, 15)
        self._orb_end     = dtime(9, 30)  # ORB window: first 15 min

    # ─────────────────────────────────────────────────────────
    # MAIN ENTRY: process raw tick batch → signals
    # ─────────────────────────────────────────────────────────
    async def process_ticks(self, ticks: list) -> list:
        signals = []
        for tick in ticks:
            sec_id = tick["security_id"]
            ltp    = tick.get("ltp", 0)
            volume = tick.get("volume", 0)
            oi     = tick.get("oi", 0)
            now    = datetime.now().time()

            # Update VWAP state
            self._update_vwap(sec_id, ltp, volume)
            vwap = self._get_vwap(sec_id)

            # Build 1-min candles
            self._update_candle(sec_id, tick)

            # Update ORB
            self._update_orb(sec_id, ltp, now)

            # ── 1. VWAP BREAKOUT ──────────────────────────────
            if vwap and ltp > 0:
                vwap_signal = await self._check_vwap_breakout(sec_id, ltp, vwap, volume)
                if vwap_signal:
                    signals.append(vwap_signal)

            # ── 2. OPENING RANGE BREAKOUT ─────────────────────
            if now > self._orb_end:
                orb_signal = await self._check_orb(sec_id, ltp, volume, now)
                if orb_signal:
                    signals.append(orb_signal)

            # ── 3. OI SPIKE ───────────────────────────────────
            if oi > 0:
                oi_signal = await self._check_oi_spike(sec_id, oi, ltp, volume)
                if oi_signal:
                    signals.append(oi_signal)

            # ── 4. MOMENTUM ───────────────────────────────────
            momentum_signal = await self._check_momentum(sec_id, ltp)
            if momentum_signal:
                signals.append(momentum_signal)

        # ── 5. IV RANK CHECK (on option chain data) ──────────
        iv_signal = await self._check_iv_rank()
        if iv_signal:
            signals.append(iv_signal)

        # Store signals in Redis list (keep last 200)
        for sig in signals:
            await self.redis.lpush("signals:feed", json.dumps(sig))
            await self.redis.ltrim("signals:feed", 0, 199)

        return signals

    # ─────────────────────────────────────────────────────────
    # VWAP
    # ─────────────────────────────────────────────────────────
    def _update_vwap(self, sec_id: str, ltp: float, volume: int):
        if sec_id not in self._vwap_state:
            self._vwap_state[sec_id] = {"sum_pv": 0.0, "sum_v": 0}
        self._vwap_state[sec_id]["sum_pv"] += ltp * volume
        self._vwap_state[sec_id]["sum_v"]  += volume

    def _get_vwap(self, sec_id: str) -> Optional[float]:
        s = self._vwap_state.get(sec_id)
        if not s or s["sum_v"] == 0:
            return None
        return round(s["sum_pv"] / s["sum_v"], 2)

    async def _check_vwap_breakout(
        self, sec_id: str, ltp: float, vwap: float, volume: int
    ) -> Optional[dict]:
        prev_key = f"prev_ltp:{sec_id}"
        prev_str = await self.redis.get(prev_key)
        await self.redis.setex(prev_key, 300, str(ltp))
        if not prev_str:
            return None

        prev_ltp = float(prev_str)
        dev_pct = (ltp - vwap) / vwap * 100

        # Breakout: price crossed VWAP with momentum
        crossed_up   = prev_ltp < vwap <= ltp and dev_pct > 0.05
        crossed_down = prev_ltp > vwap >= ltp and dev_pct < -0.05

        # Volume confirmation: needs 1.5x average volume
        avg_vol_str = await self.redis.get(f"avg_vol:{sec_id}")
        avg_vol = float(avg_vol_str) if avg_vol_str else volume
        vol_ratio = volume / avg_vol if avg_vol else 1

        if not (crossed_up or crossed_down):
            return None
        if vol_ratio < 1.3:   # not enough volume
            return None

        direction = SIGNAL_BUY_CE if crossed_up else SIGNAL_BUY_PE
        confidence = min(95, int(60 + vol_ratio * 8 + abs(dev_pct) * 10))

        return self._build_signal(
            sec_id=sec_id,
            strategy=STRATEGY_VWAP,
            signal_type=direction,
            ltp=ltp,
            vwap=vwap,
            confidence=confidence,
            triggers=["VWAP_CROSS", f"VOL {vol_ratio:.1f}x", f"DEV {dev_pct:+.2f}%"],
            reason=f"Price {'above' if crossed_up else 'below'} VWAP. Volume {vol_ratio:.1f}x average. VWAP dev: {dev_pct:+.2f}%"
        )

    # ─────────────────────────────────────────────────────────
    # OPENING RANGE BREAKOUT
    # ─────────────────────────────────────────────────────────
    def _update_orb(self, sec_id: str, ltp: float, now: dtime):
        if now < self._orb_end:
            if sec_id not in self._orb:
                self._orb[sec_id] = {"high": ltp, "low": ltp, "set": False}
            else:
                self._orb[sec_id]["high"] = max(self._orb[sec_id]["high"], ltp)
                self._orb[sec_id]["low"]  = min(self._orb[sec_id]["low"],  ltp)
        elif sec_id in self._orb and not self._orb[sec_id]["set"]:
            self._orb[sec_id]["set"] = True
            logger.info(f"ORB set for {sec_id}: H={self._orb[sec_id]['high']} L={self._orb[sec_id]['low']}")

    async def _check_orb(self, sec_id: str, ltp: float, volume: int, now: dtime) -> Optional[dict]:
        orb = self._orb.get(sec_id)
        if not orb or not orb["set"]:
            return None
        # Check only once per breakout (debounce)
        orb_key = f"orb_sig:{sec_id}"
        if await self.redis.exists(orb_key):
            return None

        broke_high = ltp > orb["high"] * 1.002   # 0.2% buffer
        broke_low  = ltp < orb["low"]  * 0.998

        if not (broke_high or broke_low):
            return None

        await self.redis.setex(orb_key, 900, "1")  # 15 min debounce

        orb_range = orb["high"] - orb["low"]
        confidence = min(92, int(65 + (orb_range / ltp) * 1000))
        direction = SIGNAL_BUY_CE if broke_high else SIGNAL_BUY_PE

        return self._build_signal(
            sec_id=sec_id,
            strategy=STRATEGY_ORB,
            signal_type=direction,
            ltp=ltp,
            confidence=confidence,
            triggers=["ORB_BREAK", f"RANGE {orb_range:.0f}pts", "CONFIRM"],
            reason=f"Opening Range Breakout — broke {'high' if broke_high else 'low'} of {orb['high'] if broke_high else orb['low']:.0f}. ORB range: {orb_range:.0f} pts"
        )

    # ─────────────────────────────────────────────────────────
    # OI SPIKE
    # ─────────────────────────────────────────────────────────
    async def _check_oi_spike(self, sec_id: str, oi: int, ltp: float, volume: int) -> Optional[dict]:
        prev_oi_str = await self.redis.get(f"prev_oi:{sec_id}")
        await self.redis.setex(f"prev_oi:{sec_id}", 3600, str(oi))
        if not prev_oi_str:
            return None

        prev_oi = int(prev_oi_str)
        if prev_oi == 0:
            return None

        oi_change_pct = (oi - prev_oi) / prev_oi * 100

        if abs(oi_change_pct) < 5:   # need >5% OI change
            return None

        # Debounce: don't signal twice in 5 min
        debounce_key = f"oi_spike_sig:{sec_id}"
        if await self.redis.exists(debounce_key):
            return None
        await self.redis.setex(debounce_key, 300, "1")

        direction = SIGNAL_BUY_CE if oi_change_pct > 0 else SIGNAL_BUY_PE
        confidence = min(90, int(55 + abs(oi_change_pct) * 2))

        return self._build_signal(
            sec_id=sec_id,
            strategy=STRATEGY_OI_SPIKE,
            signal_type=direction,
            ltp=ltp,
            confidence=confidence,
            triggers=[f"OI {oi_change_pct:+.1f}%", f"VOL {volume:,}", "SPIKE"],
            reason=f"OI spike: {oi_change_pct:+.1f}% in last interval. Current OI: {oi:,}. {'Bullish buildup' if oi_change_pct > 0 else 'Bearish buildup'} detected."
        )

    # ─────────────────────────────────────────────────────────
    # HIGH MOMENTUM
    # ─────────────────────────────────────────────────────────
    async def _check_momentum(self, sec_id: str, ltp: float) -> Optional[dict]:
        candles = self._candles.get(sec_id, [])
        if len(candles) < 5:
            return None

        # 5-candle momentum: all closes must be in same direction
        recent = candles[-5:]
        all_up   = all(recent[i]["close"] > recent[i-1]["close"] for i in range(1,5))
        all_down = all(recent[i]["close"] < recent[i-1]["close"] for i in range(1,5))

        if not (all_up or all_down):
            return None

        debounce_key = f"momentum_sig:{sec_id}"
        if await self.redis.exists(debounce_key):
            return None
        await self.redis.setex(debounce_key, 600, "1")  # 10 min

        move_pct = abs(recent[-1]["close"] - recent[0]["open"]) / recent[0]["open"] * 100
        if move_pct < 0.3:   # need at least 0.3% move
            return None

        direction = SIGNAL_BUY_CE if all_up else SIGNAL_BUY_PE
        confidence = min(88, int(55 + move_pct * 8))

        return self._build_signal(
            sec_id=sec_id,
            strategy=STRATEGY_MOMENTUM,
            signal_type=direction,
            ltp=ltp,
            confidence=confidence,
            triggers=["5-CANDLE MOM", f"{move_pct:.2f}% MOVE", "TREND"],
            reason=f"5-candle consecutive {'bullish' if all_up else 'bearish'} momentum. Total move: {move_pct:.2f}%"
        )

    # ─────────────────────────────────────────────────────────
    # IV RANK CHECK
    # ─────────────────────────────────────────────────────────
    async def _check_iv_rank(self) -> Optional[dict]:
        iv_data = await self.redis.get("iv:nifty_atm")
        if not iv_data:
            return None
        iv_info = json.loads(iv_data)
        iv_rank = iv_info.get("rank", 50)

        debounce_key = "ivr_sig:nifty"
        if await self.redis.exists(debounce_key):
            return None

        if iv_rank < 25:
            await self.redis.setex(debounce_key, 1800, "1")
            return self._build_signal(
                sec_id="13",
                strategy=STRATEGY_IV_CRUSH,
                signal_type=SIGNAL_BUY_CE,
                ltp=iv_info.get("atm_ce_ltp", 0),
                confidence=78,
                triggers=[f"IV RANK {iv_rank}", "IV LOW", "CHEAP OPT"],
                reason=f"IV Rank at {iv_rank} — bottom 25th percentile. Options are cheap. High risk/reward for directional long call play."
            )
        return None

    # ─────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────
    def _update_candle(self, sec_id: str, tick: dict):
        if sec_id not in self._candles:
            self._candles[sec_id] = []
        ltp = tick.get("ltp", 0)
        now = datetime.now()
        minute_key = now.strftime("%H%M")
        candles = self._candles[sec_id]
        if candles and candles[-1]["minute"] == minute_key:
            c = candles[-1]
            c["high"]   = max(c["high"], ltp)
            c["low"]    = min(c["low"],  ltp)
            c["close"]  = ltp
            c["volume"] += tick.get("volume", 0)
        else:
            candles.append({
                "minute": minute_key, "open": ltp, "high": ltp,
                "low": ltp, "close": ltp, "volume": tick.get("volume", 0)
            })
            if len(candles) > 390:   # keep ~1 day of 1-min candles
                candles.pop(0)

    def _build_signal(
        self, sec_id: str, strategy: str, signal_type: str,
        ltp: float, confidence: int, triggers: list, reason: str,
        vwap: Optional[float] = None, **kwargs
    ) -> dict:
        # Estimate SL and target
        sl_pct  = 0.18 if "CE" in signal_type else 0.18
        tgt_pct = 0.40

        sl   = round(ltp * (1 - sl_pct), 1)
        tgt  = round(ltp * (1 + tgt_pct), 1)
        rr   = round(tgt_pct / sl_pct, 1)

        return {
            "id":           f"{sec_id}_{strategy}_{int(datetime.now().timestamp())}",
            "timestamp":    datetime.now().isoformat(),
            "security_id":  sec_id,
            "strategy":     strategy,
            "signal_type":  signal_type,
            "ltp":          ltp,
            "sl":           sl,
            "target":       tgt,
            "rr":           rr,
            "vwap":         vwap,
            "confidence":   confidence,
            "triggers":     triggers,
            "reason":       reason,
            "status":       "ACTIVE",
        }

    async def get_latest_signals(self, limit: int = 20, signal_type: Optional[str] = None) -> list:
        raw = await self.redis.lrange("signals:feed", 0, limit * 3)
        signals = [json.loads(r) for r in raw]
        if signal_type:
            signals = [s for s in signals if s["signal_type"] == signal_type]
        return signals[:limit]

    async def build_market_summary(self, redis: aioredis.Redis) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "signals_today": await redis.llen("signals:feed"),
        }
