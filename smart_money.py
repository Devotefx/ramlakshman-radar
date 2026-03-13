"""
DHAN ALPHA — Supporting Services
  - SmartMoneyDetector  : OI+Price pattern analysis
  - NewsParser          : Headline detection + signal generation
  - ExpiryModule        : Gamma squeeze, max pain, pinning
  - ConnectionManager   : WebSocket broadcast hub
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional
import aiohttp
import redis.asyncio as aioredis
from fastapi import WebSocket

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. SMART MONEY DETECTOR
# ═══════════════════════════════════════════════════════════════
class SmartMoneyDetector:
    """
    Interprets Price + OI combinations:
      Price↑ + OI↑ → Long buildup   (BUY CE)
      Price↓ + OI↑ → Short buildup  (BUY PE)
      Price↑ + OI↓ → Short covering (BUY CE weak)
      Price↓ + OI↓ → Long unwinding (BUY PE weak)
    """

    LONG_BUILDUP  = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    SHORT_COVER   = "SHORT_COVERING"
    LONG_UNWIND   = "LONG_UNWINDING"

    SIGNAL_MAP = {
        LONG_BUILDUP:  {"type": "BUY_CE", "strength": "STRONG", "color": "green"},
        SHORT_BUILDUP: {"type": "BUY_PE", "strength": "STRONG", "color": "red"},
        SHORT_COVER:   {"type": "BUY_CE", "strength": "WEAK",   "color": "amber"},
        LONG_UNWIND:   {"type": "BUY_PE", "strength": "WEAK",   "color": "purple"},
    }

    async def analyze(self, ticks: list, redis: aioredis.Redis) -> list:
        signals = []
        for tick in ticks:
            sec_id = tick.get("security_id")
            ltp    = tick.get("ltp", 0)
            oi     = tick.get("oi", 0)
            if not ltp or not oi:
                continue

            prev_ltp_s = await redis.get(f"sm_prev_ltp:{sec_id}")
            prev_oi_s  = await redis.get(f"sm_prev_oi:{sec_id}")

            await redis.setex(f"sm_prev_ltp:{sec_id}", 3600, str(ltp))
            await redis.setex(f"sm_prev_oi:{sec_id}",  3600, str(oi))

            if not prev_ltp_s or not prev_oi_s:
                continue

            prev_ltp = float(prev_ltp_s)
            prev_oi  = int(float(prev_oi_s))

            price_up = ltp > prev_ltp * 1.001     # >0.1% up
            price_dn = ltp < prev_ltp * 0.999     # >0.1% down
            oi_up    = oi > prev_oi * 1.02        # >2% OI increase
            oi_dn    = oi < prev_oi * 0.98        # >2% OI decrease

            pattern = None
            if price_up and oi_up:   pattern = self.LONG_BUILDUP
            elif price_dn and oi_up: pattern = self.SHORT_BUILDUP
            elif price_up and oi_dn: pattern = self.SHORT_COVER
            elif price_dn and oi_dn: pattern = self.LONG_UNWIND

            if pattern:
                meta = self.SIGNAL_MAP[pattern]
                oi_chg_pct = (oi - prev_oi) / prev_oi * 100
                sig = {
                    "security_id": sec_id,
                    "pattern":     pattern,
                    "signal_type": meta["type"],
                    "strength":    meta["strength"],
                    "color":       meta["color"],
                    "ltp":         ltp,
                    "oi":          oi,
                    "oi_change":   round(oi_chg_pct, 2),
                    "price_change": round((ltp - prev_ltp) / prev_ltp * 100, 3),
                    "timestamp":   datetime.now().isoformat(),
                    "label": {
                        self.LONG_BUILDUP:  f"Price↑ + OI↑ → LONG BUILD",
                        self.SHORT_BUILDUP: f"Price↓ + OI↑ → SHORT BUILD",
                        self.SHORT_COVER:   f"Price↑ + OI↓ → SHORT COVER",
                        self.LONG_UNWIND:   f"Price↓ + OI↓ → LONG UNWIND",
                    }[pattern],
                }
                signals.append(sig)
                # Cache for dashboard
                await redis.setex(
                    f"sm_signal:{sec_id}",
                    300,
                    json.dumps(sig)
                )
        # Save latest batch
        if signals:
            await redis.setex("smart_money:latest", 60, json.dumps(signals[-20:]))
        return signals


# ═══════════════════════════════════════════════════════════════
# 2. NEWS PARSER
# ═══════════════════════════════════════════════════════════════
HEADLINE_KEYWORDS = {
    # Bullish catalysts → BUY CE
    "BULLISH": [
        "beats estimate", "profit rises", "acquisition", "FII buying",
        "rate cut", "policy easing", "strong results", "record revenue",
        "dividend", "buyback", "stake acquisition", "upgrade"
    ],
    # Bearish catalysts → BUY PE
    "BEARISH": [
        "guidance cut", "profit falls", "sebi penalty", "npa rises",
        "rate hike", "margin compression", "earnings miss", "recall",
        "fraud", "downgrade", "stake sale", "investigation"
    ],
    # Neutral/monitor
    "NEUTRAL": [
        "rbi policy", "sebi circular", "budget", "gst", "fpo", "ipo",
        "merger", "demerger", "quarterly results"
    ]
}

VOLUME_SPIKE_THRESHOLD = 2.5  # 2.5x average = spike
EXTREME_SPIKE          = 4.0  # 4x = extreme


class NewsParser:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis
        # Free news sources (replace with premium feed in production)
        self.sources = [
            "https://feeds.feedburner.com/ndtvprofit-latest",
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        ]

    async def scan_and_signal(self) -> list:
        """Fetch news, parse for market impact, generate signals."""
        signals = []
        headlines = await self._fetch_headlines()
        for item in headlines:
            sig = self._parse_headline(item)
            if sig:
                # Deduplicate
                key = f"news_sig:{hash(item['title'])}"
                if not await self.redis.exists(key):
                    await self.redis.setex(key, 3600, "1")
                    signals.append(sig)
                    # Add to news feed list
                    await self.redis.lpush("news:feed", key)
                    await self.redis.ltrim("news:feed", 0, 99)
                    await self.redis.setex(f"news:{key}", 3600, json.dumps(sig))
        return signals

    async def _fetch_headlines(self) -> list:
        """Fetch headlines from RSS feeds."""
        import xml.etree.ElementTree as ET
        headlines = []
        async with aiohttp.ClientSession() as session:
            for url in self.sources:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            root = ET.fromstring(text)
                            for item in root.findall(".//item")[:10]:
                                title = item.findtext("title", "")
                                desc  = item.findtext("description", "")
                                headlines.append({
                                    "title": title,
                                    "description": desc,
                                    "source": url,
                                    "timestamp": datetime.now().isoformat()
                                })
                except Exception as e:
                    logger.debug(f"News fetch error {url}: {e}")
        return headlines

    def _parse_headline(self, item: dict) -> Optional[dict]:
        title = (item.get("title") or "").lower()
        desc  = (item.get("description") or "").lower()
        combined = title + " " + desc

        bias = "NEUTRAL"
        matched_keywords = []

        for kw in HEADLINE_KEYWORDS["BULLISH"]:
            if kw in combined:
                bias = "BULLISH"
                matched_keywords.append(kw)
        for kw in HEADLINE_KEYWORDS["BEARISH"]:
            if kw in combined:
                bias = "BEARISH"
                matched_keywords.append(kw)

        if bias == "NEUTRAL" or not matched_keywords:
            return None

        # Try to extract stock name (rough heuristic — replace with NLP in prod)
        stock = "MARKET"
        for known in ["nifty", "sensex", "banknifty", "hdfc", "reliance",
                      "infosys", "tcs", "sbi", "icici", "kotak", "axis"]:
            if known in combined:
                stock = known.upper()
                break

        signal_type = "BUY_CE" if bias == "BULLISH" else "BUY_PE"

        return {
            "type":         "NEWS_REACTION",
            "signal_type":  signal_type,
            "stock":        stock,
            "headline":     item["title"][:120],
            "bias":         bias,
            "keywords":     matched_keywords[:3],
            "category":     self._categorize(combined),
            "confidence":   72 + len(matched_keywords) * 4,
            "vol_expected": "HIGH",
            "timestamp":    datetime.now().isoformat(),
            "action":       f"Buy {'ATM CE' if bias == 'BULLISH' else 'ATM PE'} on {stock} — monitor 1-min vol spike",
        }

    def _categorize(self, text: str) -> str:
        cats = {
            "rbi":    ["rbi", "repo rate", "monetary policy"],
            "sebi":   ["sebi", "circular", "regulation"],
            "result": ["quarterly", "q1", "q2", "q3", "q4", "results", "earnings"],
            "merger": ["merger", "acquisition", "stake"],
            "policy": ["budget", "fiscal", "government"],
        }
        for cat, keywords in cats.items():
            if any(kw in text for kw in keywords):
                return cat.upper()
        return "GENERAL"


# ═══════════════════════════════════════════════════════════════
# 3. EXPIRY MODULE
# ═══════════════════════════════════════════════════════════════
class ExpiryModule:
    """
    Thursday Expiry Squeeze Detector.
    Identifies: Gamma squeeze zones, pinning strikes, dealer hedging pressure.
    """

    async def analyze(self, underlying: str, chain: dict) -> dict:
        if not chain or not chain.get("strikes"):
            return {}

        spot       = chain.get("spot", 0)
        strikes    = chain.get("strikes", [])
        max_pain   = chain.get("max_pain", 0)
        pcr        = chain.get("pcr", 1)

        gamma_zones     = self._find_gamma_zones(strikes, spot)
        pinning_strikes = self._find_pinning_strikes(strikes, spot)
        dealer_hedge    = self._estimate_dealer_pressure(strikes, spot)
        squeeze_signal  = self._detect_squeeze(spot, max_pain, gamma_zones, strikes)

        return {
            "underlying":       underlying,
            "spot":             spot,
            "max_pain":         max_pain,
            "spot_to_maxpain":  round(spot - max_pain, 1),
            "pcr":              pcr,
            "gamma_zones":      gamma_zones,
            "pinning_strikes":  pinning_strikes,
            "dealer_pressure":  dealer_hedge,
            "squeeze_signal":   squeeze_signal,
            "expiry_mode":      datetime.now().weekday() == 3,  # Thursday
            "timestamp":        datetime.now().isoformat(),
        }

    def _find_gamma_zones(self, strikes: list, spot: float) -> list:
        """Strikes with highest gamma (near-ATM with highest OI)."""
        zones = []
        atm = min(strikes, key=lambda s: abs(s["strike"] - spot))
        for s in strikes:
            dist = abs(s["strike"] - spot)
            if dist > spot * 0.02:  # within 2% of spot
                continue
            total_gamma = (s.get("ce_gamma", 0) * s.get("ce_oi", 0) +
                           s.get("pe_gamma", 0) * s.get("pe_oi", 0))
            if total_gamma > 0:
                zones.append({
                    "strike": s["strike"],
                    "gamma_notional": round(total_gamma, 4),
                    "type": "ATM" if s["strike"] == atm["strike"] else "NEAR_ATM",
                    "label": f"{s['strike']} γ-WALL",
                })
        zones.sort(key=lambda z: z["gamma_notional"], reverse=True)
        return zones[:5]

    def _find_pinning_strikes(self, strikes: list, spot: float) -> list:
        """Strikes where max CE+PE OI concentration exists — spot tends to pin here."""
        pins = []
        for s in strikes:
            total_oi = s.get("ce_oi", 0) + s.get("pe_oi", 0)
            dist_pct = abs(s["strike"] - spot) / spot * 100
            if dist_pct < 1.5 and total_oi > 0:
                pins.append({
                    "strike":    s["strike"],
                    "total_oi":  total_oi,
                    "dist_pct":  round(dist_pct, 2),
                    "pin_score": round(total_oi / (1 + dist_pct), 0),
                })
        pins.sort(key=lambda p: p["pin_score"], reverse=True)
        return pins[:3]

    def _estimate_dealer_pressure(self, strikes: list, spot: float) -> dict:
        """
        Estimate net dealer gamma (GEX).
        Positive GEX → dealers are long gamma → they SELL rallies, BUY dips (stabilizing)
        Negative GEX → dealers are short gamma → they BUY rallies, SELL dips (accelerating)
        """
        gex = 0
        for s in strikes:
            k   = s["strike"]
            dist = (spot - k) / spot
            # CE: dealer sold call → long gamma
            ce_gex = s.get("ce_gamma", 0) * s.get("ce_oi", 0) * spot * 100
            # PE: dealer sold put → long gamma (below spot = negative contribution)
            pe_gex = s.get("pe_gamma", 0) * s.get("pe_oi", 0) * spot * 100
            if k > spot:
                gex += ce_gex
            else:
                gex -= pe_gex

        return {
            "gex":       round(gex, 0),
            "regime":    "STABILIZING" if gex > 0 else "ACCELERATING",
            "bias":      "RANGE_BOUND" if gex > 0 else "TRENDING",
            "note": (
                "Dealers long gamma — expect mean reversion, range-bound moves"
                if gex > 0 else
                "Dealers short gamma — expect large directional moves, breakouts"
            )
        }

    def _detect_squeeze(self, spot: float, max_pain: float, gamma_zones: list, strikes: list) -> dict:
        """Identify gamma squeeze setup."""
        dist_to_maxpain = spot - max_pain
        abs_dist = abs(dist_to_maxpain)

        if abs_dist < spot * 0.003:   # within 0.3% → pinning
            return {
                "type": "PINNING",
                "signal": "NEUTRAL",
                "note": f"Spot pinning at {spot:.0f} — very close to max pain {max_pain:.0f}"
            }
        elif abs_dist < spot * 0.01:  # within 1% → gravitation
            direction = "CE" if dist_to_maxpain > 0 else "PE"
            return {
                "type": "MAX_PAIN_GRAVITY",
                "signal": f"BUY_{direction}",
                "note": f"Max pain gravity: spot will likely drift {'down' if dist_to_maxpain > 0 else 'up'} to {max_pain:.0f}",
                "target_strike": max_pain,
            }
        else:
            return {
                "type": "GAMMA_SQUEEZE_POTENTIAL",
                "signal": "BUY_CE" if dist_to_maxpain < 0 else "BUY_PE",
                "note": f"Spot {abs_dist:.0f} pts from max pain — potential squeeze if spot reaches gamma wall",
            }

    async def detect_squeeze(self, redis: aioredis.Redis) -> list:
        """Run on Thursday — returns live squeeze signals."""
        signals = []
        for underlying in ["NIFTY", "BANKNIFTY"]:
            chain_data = await redis.get(f"chain:{underlying}:latest")
            if chain_data:
                chain = json.loads(chain_data)
                result = await self.analyze(underlying, chain)
                if result.get("squeeze_signal", {}).get("signal") in ("BUY_CE", "BUY_PE"):
                    signals.append(result)
        return signals


# ═══════════════════════════════════════════════════════════════
# 4. WEBSOCKET CONNECTION MANAGER
# ═══════════════════════════════════════════════════════════════
class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        """Send to all connected clients. Auto-removes dead connections."""
        message = json.dumps(data)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            self.disconnect(ws)
