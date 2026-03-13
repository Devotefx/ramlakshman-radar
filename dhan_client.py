"""
Dhan API Client
Handles: REST (option chain, historical, orders) + WebSocket (live ticks)
Dhan LiveAPI docs: https://dhanhq.co/docs/v2/
"""
import asyncio
import json
import logging
import struct
import time
from datetime import datetime, date
from typing import AsyncGenerator, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONSTANTS (Dhan API)
# ─────────────────────────────────────────────────────────────
DHAN_REST_BASE = "https://api.dhan.co/v2"
DHAN_WS_URL    = "wss://api-feed.dhan.co"

EXCHANGE_NSE   = "NSE_EQ"
EXCHANGE_NFO   = "NSE_FNO"
SEGMENT_NFO    = 2   # F&O
SEGMENT_NSE    = 1   # Cash

# Feed packet types (Dhan binary protocol)
PACKET_TICKER   = 2   # LTP only
PACKET_QUOTE    = 4   # LTP + OHLC + volume
PACKET_FULL     = 8   # Quote + 20-depth + OI

# Exchange indices (Dhan segment map)
UNDERLYING_MAP = {
    "NIFTY":    {"security_id": "13", "exchange": "NSE_FNO"},
    "BANKNIFTY":{"security_id": "25",  "exchange": "NSE_FNO"},
    "FINNIFTY": {"security_id": "27",  "exchange": "NSE_FNO"},
}


class DhanClient:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.access_token = access_token
        self._session: Optional[aiohttp.ClientSession] = None
        self._subscribed: set = set()
        self._ws = None

    @property
    def headers(self) -> dict:
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    # ──────────────────────────────────
    # REST: OPTION CHAIN
    # ──────────────────────────────────
    async def get_option_chain(self, underlying: str, expiry_date: Optional[str] = None) -> dict:
        """
        Fetch full option chain from Dhan.
        Returns: {strikes: [...], metadata: {...}}
        """
        session = await self._get_session()
        params = {"UnderlyingScrip": UNDERLYING_MAP.get(underlying, {}).get("security_id", "13")}
        if expiry_date:
            params["ExpiryDate"] = expiry_date

        try:
            async with session.get(f"{DHAN_REST_BASE}/optionchain", params=params) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    return self._parse_option_chain(raw, underlying)
                else:
                    text = await resp.text()
                    logger.error(f"Option chain error {resp.status}: {text}")
                    return self._mock_option_chain(underlying)
        except Exception as e:
            logger.error(f"Option chain fetch failed: {e}")
            return self._mock_option_chain(underlying)

    def _parse_option_chain(self, raw: dict, underlying: str) -> dict:
        """Parse Dhan option chain response into normalized format."""
        strikes = []
        oc_data = raw.get("data", {})
        for strike_price, data in oc_data.items():
            ce = data.get("CE", {})
            pe = data.get("PE", {})
            strikes.append({
                "strike":        float(strike_price),
                "ce_ltp":        ce.get("last_price", 0),
                "ce_oi":         ce.get("oi", 0),
                "ce_oi_change":  ce.get("oi_change", 0),
                "ce_volume":     ce.get("volume", 0),
                "ce_iv":         ce.get("implied_volatility", 0),
                "ce_delta":      ce.get("delta", 0),
                "ce_gamma":      ce.get("gamma", 0),
                "ce_theta":      ce.get("theta", 0),
                "ce_vega":       ce.get("vega", 0),
                "pe_ltp":        pe.get("last_price", 0),
                "pe_oi":         pe.get("oi", 0),
                "pe_oi_change":  pe.get("oi_change", 0),
                "pe_volume":     pe.get("volume", 0),
                "pe_iv":         pe.get("implied_volatility", 0),
                "pe_delta":      pe.get("delta", 0),
                "pe_gamma":      pe.get("gamma", 0),
                "pe_theta":      pe.get("theta", 0),
                "pe_vega":       pe.get("vega", 0),
                "expiry":        ce.get("expiry_date") or pe.get("expiry_date"),
            })
        strikes.sort(key=lambda x: x["strike"])
        total_ce_oi = sum(s["ce_oi"] for s in strikes)
        total_pe_oi = sum(s["pe_oi"] for s in strikes)
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0
        max_pain = self._calculate_max_pain(strikes)
        return {
            "underlying": underlying,
            "spot": raw.get("data", {}).get("underlyingValue", 0),
            "strikes": strikes,
            "pcr": pcr,
            "max_pain": max_pain,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "timestamp": datetime.now().isoformat(),
        }

    def _calculate_max_pain(self, strikes: list) -> float:
        """Max pain: strike where total option buyers lose most money."""
        if not strikes:
            return 0
        min_loss = float("inf")
        max_pain_strike = 0
        for test in strikes:
            s = test["strike"]
            loss = 0
            for opt in strikes:
                k = opt["strike"]
                # CE writers' loss if spot > strike
                if s > k:
                    loss += (s - k) * opt["ce_oi"]
                # PE writers' loss if spot < strike
                elif s < k:
                    loss += (k - s) * opt["pe_oi"]
            if loss < min_loss:
                min_loss = loss
                max_pain_strike = s
        return max_pain_strike

    def _mock_option_chain(self, underlying: str) -> dict:
        """Returns synthetic data when API not configured (dev mode)."""
        import random
        base = {"NIFTY": 24800, "BANKNIFTY": 53200, "FINNIFTY": 23100}.get(underlying, 24800)
        strikes = []
        for i in range(-8, 9):
            s = base + i * 50
            dist = abs(i)
            strikes.append({
                "strike": s, "expiry": "2025-03-27",
                "ce_ltp": max(1, 150 - dist*18 + random.randint(-5,5)),
                "ce_oi": random.randint(5e4, 8e5),
                "ce_oi_change": random.randint(-5e4, 1e5),
                "ce_volume": random.randint(1e4, 5e5),
                "ce_iv": round(12 + dist*0.8 + random.uniform(-0.5,0.5), 1),
                "ce_delta": round(max(0.05, 0.5 - i*0.06), 2),
                "ce_gamma": round(0.002 + random.uniform(0,0.003), 4),
                "ce_theta": round(-(3 + dist*0.5), 1),
                "ce_vega": round(12 - dist*0.5, 1),
                "pe_ltp": max(1, 150 - dist*18 + random.randint(-5,5)),
                "pe_oi": random.randint(5e4, 9e5),
                "pe_oi_change": random.randint(-4e4, 8e4),
                "pe_volume": random.randint(1e4, 5e5),
                "pe_iv": round(13 + dist*0.9 + random.uniform(-0.5,0.5), 1),
                "pe_delta": round(max(-0.95, -0.5 - i*0.06), 2),
                "pe_gamma": round(0.002 + random.uniform(0,0.003), 4),
                "pe_theta": round(-(3 + dist*0.5), 1),
                "pe_vega": round(12 - dist*0.5, 1),
            })
        total_ce = sum(s["ce_oi"] for s in strikes)
        total_pe = sum(s["pe_oi"] for s in strikes)
        return {
            "underlying": underlying, "spot": base, "strikes": strikes,
            "pcr": round(total_pe/total_ce, 3), "max_pain": base - 50,
            "total_ce_oi": total_ce, "total_pe_oi": total_pe,
            "timestamp": datetime.now().isoformat(),
        }

    # ──────────────────────────────────
    # REST: HISTORICAL DATA
    # ──────────────────────────────────
    async def get_historical_data(
        self,
        security_id: str,
        exchange: str,
        instrument_type: str,
        expiry_code: int,
        from_date: str,
        to_date: str,
        frequency: str = "1"  # 1,3,5,10,15,25,60 minutes; "D" for daily
    ) -> list:
        session = await self._get_session()
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange,
            "instrument": instrument_type,
            "expiryCode": expiry_code,
            "oi": True,
            "fromDate": from_date,
            "toDate": to_date,
        }
        endpoint = "intraday" if frequency.isdigit() else "daily"
        if frequency.isdigit():
            payload["interval"] = frequency
        try:
            async with session.post(f"{DHAN_REST_BASE}/charts/{endpoint}", json=payload) as resp:
                if resp.status == 200:
                    return await resp.json()
                return []
        except Exception as e:
            logger.error(f"Historical data error: {e}")
            return []

    # ──────────────────────────────────
    # REST: PLACE ORDER
    # ──────────────────────────────────
    async def place_order(
        self,
        security_id: str,
        exchange: str,
        transaction_type: str,  # "BUY" | "SELL"
        order_type: str,        # "MARKET" | "LIMIT" | "SL" | "SL-M"
        quantity: int,
        price: float = 0,
        trigger_price: float = 0,
        product_type: str = "INTRADAY",
        validity: str = "DAY",
    ) -> dict:
        session = await self._get_session()
        payload = {
            "dhanClientId":   self.client_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange,
            "productType":     product_type,
            "orderType":       order_type,
            "validity":        validity,
            "tradingSymbol":   "",
            "securityId":      security_id,
            "quantity":        quantity,
            "price":           price,
            "triggerPrice":    trigger_price,
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
            "amoTime":         "OPEN",
            "boProfitValue":   0,
            "boStopLossValue": 0,
        }
        try:
            async with session.post(f"{DHAN_REST_BASE}/orders", json=payload) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Order placement error: {e}")
            return {"error": str(e)}

    # ──────────────────────────────────
    # WEBSOCKET: LIVE TICK STREAM
    # ──────────────────────────────────
    async def subscribe_instruments(self, instruments: list):
        """Subscribe list of {security_id, exchange_segment} to live feed."""
        self._subscribed.update([i["security_id"] for i in instruments])

    async def stream_ticks(self) -> AsyncGenerator[list, None]:
        """
        Yields batches of parsed tick dictionaries from Dhan WebSocket.
        Protocol: JSON subscribe, binary data frames.
        """
        subscribe_msg = {
            "RequestCode": 15,
            "InstrumentCount": 1,
            "InstrumentList": [
                {"ExchangeSegment": "NSE_FNO", "SecurityId": "13"},   # NIFTY
                {"ExchangeSegment": "NSE_FNO", "SecurityId": "25"},   # BANKNIFTY
                {"ExchangeSegment": "NSE_FNO", "SecurityId": "27"},   # FINNIFTY
            ]
        }
        while True:
            try:
                async with websockets.connect(
                    DHAN_WS_URL,
                    extra_headers={
                        "access-token": self.access_token,
                        "client-id": self.client_id,
                    },
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("📡 Dhan WebSocket connected")
                    async for message in ws:
                        if isinstance(message, bytes):
                            ticks = self._parse_binary_packet(message)
                            if ticks:
                                yield ticks
                        else:
                            data = json.loads(message)
                            logger.debug(f"WS message: {data}")
            except ConnectionClosed as e:
                logger.warning(f"WS closed: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def _parse_binary_packet(self, data: bytes) -> list:
        """
        Parse Dhan binary WebSocket frame.
        Format (per instrument):
          Byte 0:    Packet type (2=ticker, 4=quote, 8=full)
          Byte 1-2:  Exchange segment
          Byte 3-6:  Security ID
          Byte 7-14: LTP (double, 8 bytes)
          Byte 15-22: LTT timestamp
          ... (additional fields per packet type)
        """
        ticks = []
        offset = 0
        try:
            while offset < len(data):
                if offset + 8 > len(data):
                    break
                ptype = data[offset]
                # Skip unknown packet types
                if ptype not in (2, 4, 8):
                    break
                seg = struct.unpack_from(">H", data, offset + 1)[0]
                sec_id = struct.unpack_from(">I", data, offset + 3)[0]
                ltp = struct.unpack_from(">d", data, offset + 7)[0]
                tick = {
                    "type":        ptype,
                    "segment":     seg,
                    "security_id": str(sec_id),
                    "ltp":         round(ltp, 2),
                    "timestamp":   int(time.time() * 1000),
                }
                if ptype >= 4:   # Quote
                    tick["open"]   = struct.unpack_from(">d", data, offset + 15)[0]
                    tick["high"]   = struct.unpack_from(">d", data, offset + 23)[0]
                    tick["low"]    = struct.unpack_from(">d", data, offset + 31)[0]
                    tick["close"]  = struct.unpack_from(">d", data, offset + 39)[0]
                    tick["volume"] = struct.unpack_from(">I", data, offset + 47)[0]
                    offset += 51
                elif ptype == 2:  # Ticker only
                    offset += 15
                if ptype == 8:   # Full packet with OI + depth
                    tick["oi"]   = struct.unpack_from(">I", data, offset)[0]
                    offset += 4
                    # 20-level depth (bid/ask each: price=8B, qty=4B, orders=4B = 16B)
                    bids, asks = [], []
                    for _ in range(20):
                        p = struct.unpack_from(">d", data, offset)[0]
                        q = struct.unpack_from(">I", data, offset + 8)[0]
                        o = struct.unpack_from(">I", data, offset + 12)[0]
                        bids.append({"price": round(p,2), "qty": q, "orders": o})
                        offset += 16
                    for _ in range(20):
                        p = struct.unpack_from(">d", data, offset)[0]
                        q = struct.unpack_from(">I", data, offset + 8)[0]
                        o = struct.unpack_from(">I", data, offset + 12)[0]
                        asks.append({"price": round(p,2), "qty": q, "orders": o})
                        offset += 16
                    tick["bids"] = bids
                    tick["asks"] = asks
                ticks.append(tick)
        except (struct.error, IndexError) as e:
            logger.debug(f"Binary parse partial: {e}")
        return ticks

    async def get_positions(self) -> list:
        session = await self._get_session()
        async with session.get(f"{DHAN_REST_BASE}/positions") as resp:
            data = await resp.json()
            return data.get("data", [])

    async def get_funds(self) -> dict:
        session = await self._get_session()
        async with session.get(f"{DHAN_REST_BASE}/fundlimit") as resp:
            return await resp.json()

    async def kill_switch(self, action: str = "ACTIVATE") -> dict:
        """Kill switch: ACTIVATE halts all new orders."""
        session = await self._get_session()
        async with session.post(f"{DHAN_REST_BASE}/killswitch", json={"killSwitchStatus": action}) as resp:
            return await resp.json()
