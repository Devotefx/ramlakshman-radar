"""
DHAN ALPHA — Options Intelligence Platform
Backend Server: FastAPI + WebSocket + Signal Engine
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from typing import Optional

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from services.dhan_client import DhanClient
from services.signal_engine import SignalEngine
from services.smart_money import SmartMoneyDetector
from services.news_parser import NewsParser
from services.expiry_module import ExpiryModule
from websocket.connection_manager import ConnectionManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s — %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# LIFESPAN: boot all services
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Booting DHAN ALPHA...")
    app.state.redis = await aioredis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379"),
        encoding="utf-8", decode_responses=True
    )
    app.state.dhan = DhanClient(
        client_id=os.getenv("DHAN_CLIENT_ID", ""),
        access_token=os.getenv("DHAN_ACCESS_TOKEN", "")
    )
    app.state.manager = ConnectionManager()
    app.state.signal_engine = SignalEngine(app.state.redis)
    app.state.smart_money = SmartMoneyDetector()
    app.state.news_parser = NewsParser(app.state.redis)
    app.state.expiry_module = ExpiryModule()

    # Start background tasks
    asyncio.create_task(dhan_feed_loop(app))
    asyncio.create_task(signal_broadcast_loop(app))
    asyncio.create_task(news_scan_loop(app))

    logger.info("✅ All services running")
    yield

    await app.state.redis.close()
    logger.info("🛑 Server shutdown")


app = FastAPI(
    title="DHAN ALPHA",
    description="Hedge Fund Options Intelligence Terminal",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
app.mount("/static", StaticFiles(directory="../frontend"), name="static")


# ─────────────────────────────────────────────────────────────
# WEBSOCKET ENDPOINT
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    manager: ConnectionManager = app.state.manager
    await manager.connect(websocket)
    logger.info(f"Client connected. Total: {manager.count}")
    try:
        while True:
            data = await websocket.receive_json()
            await handle_client_message(websocket, data, app)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info(f"Client disconnected. Total: {manager.count}")


async def handle_client_message(ws: WebSocket, msg: dict, app: FastAPI):
    """Handle messages from frontend (subscribe/unsubscribe/request)"""
    msg_type = msg.get("type")
    if msg_type == "subscribe":
        instruments = msg.get("instruments", [])
        await app.state.dhan.subscribe_instruments(instruments)
    elif msg_type == "get_chain":
        underlying = msg.get("underlying", "NIFTY")
        expiry = msg.get("expiry")
        chain = await app.state.dhan.get_option_chain(underlying, expiry)
        await ws.send_json({"type": "option_chain", "data": chain})
    elif msg_type == "get_signals":
        signals = await app.state.signal_engine.get_latest_signals(limit=20)
        await ws.send_json({"type": "signals", "data": signals})


# ─────────────────────────────────────────────────────────────
# REST ENDPOINTS
# ─────────────────────────────────────────────────────────────
@app.get("/api/option-chain/{underlying}")
async def get_option_chain(underlying: str, expiry: Optional[str] = None):
    chain = await app.state.dhan.get_option_chain(underlying, expiry)
    return chain

@app.get("/api/signals")
async def get_signals(limit: int = 20, signal_type: Optional[str] = None):
    signals = await app.state.signal_engine.get_latest_signals(limit, signal_type)
    return {"signals": signals}

@app.get("/api/smart-money")
async def get_smart_money():
    data = await app.state.redis.get("smart_money:latest")
    return json.loads(data) if data else []

@app.get("/api/market-summary")
async def get_market_summary():
    data = await app.state.redis.get("market:summary")
    return json.loads(data) if data else {}

@app.get("/api/expiry-module/{underlying}")
async def get_expiry_module(underlying: str):
    result = await app.state.expiry_module.analyze(
        underlying,
        await app.state.dhan.get_option_chain(underlying)
    )
    return result

@app.get("/api/news")
async def get_news(limit: int = 10):
    keys = await app.state.redis.lrange("news:feed", 0, limit - 1)
    items = []
    for k in keys:
        d = await app.state.redis.get(f"news:{k}")
        if d: items.append(json.loads(d))
    return {"news": items}

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# ─────────────────────────────────────────────────────────────
# BACKGROUND LOOPS
# ─────────────────────────────────────────────────────────────
async def dhan_feed_loop(app: FastAPI):
    """
    Connects to Dhan WebSocket, receives tick data,
    pushes to Redis, then runs signal engine.
    """
    dhan: DhanClient = app.state.dhan
    redis = app.state.redis
    engine: SignalEngine = app.state.signal_engine
    sm: SmartMoneyDetector = app.state.smart_money
    manager: ConnectionManager = app.state.manager
    expiry: ExpiryModule = app.state.expiry_module

    async for tick_batch in dhan.stream_ticks():
        # Store raw tick in Redis (TTL 2h)
        for tick in tick_batch:
            key = f"tick:{tick['security_id']}"
            await redis.setex(key, 7200, json.dumps(tick))

        # Run signal engine
        signals = await engine.process_ticks(tick_batch)

        # Run smart money detection
        sm_signals = await sm.analyze(tick_batch, redis)

        # Detect expiry squeeze (Thursday)
        if datetime.now().weekday() == 3:  # Thursday
            expiry_sigs = await expiry.detect_squeeze(redis)
            if expiry_sigs:
                await manager.broadcast({
                    "type": "expiry_signals",
                    "data": expiry_sigs
                })

        # Broadcast to all frontend clients
        if signals:
            await manager.broadcast({
                "type": "new_signals",
                "data": signals
            })
        if sm_signals:
            await manager.broadcast({
                "type": "smart_money",
                "data": sm_signals
            })

        # Cache market summary
        summary = await engine.build_market_summary(redis)
        await redis.setex("market:summary", 30, json.dumps(summary))

        # Broadcast market summary every 5 seconds
        if int(datetime.now().timestamp()) % 5 == 0:
            await manager.broadcast({
                "type": "market_summary",
                "data": summary
            })


async def signal_broadcast_loop(app: FastAPI):
    """
    Periodically broadcasts latest option chains & signals
    to all connected clients.
    """
    manager: ConnectionManager = app.state.manager
    dhan: DhanClient = app.state.dhan
    redis = app.state.redis

    while True:
        try:
            if manager.count > 0:
                # Option chains for main indices
                for underlying in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
                    chain = await dhan.get_option_chain(underlying)
                    await manager.broadcast({
                        "type": "option_chain_update",
                        "underlying": underlying,
                        "data": chain
                    })
                    await asyncio.sleep(0.5)  # stagger requests

                # IV metrics
                iv_data = await redis.get("iv:summary")
                if iv_data:
                    await manager.broadcast({
                        "type": "iv_update",
                        "data": json.loads(iv_data)
                    })
        except Exception as e:
            logger.error(f"Broadcast loop error: {e}")
        await asyncio.sleep(10)


async def news_scan_loop(app: FastAPI):
    """Continuously scans and parses news for market-moving events."""
    news_parser: NewsParser = app.state.news_parser
    manager: ConnectionManager = app.state.manager

    while True:
        try:
            signals = await news_parser.scan_and_signal()
            if signals:
                for sig in signals:
                    await manager.broadcast({
                        "type": "news_signal",
                        "data": sig
                    })
        except Exception as e:
            logger.error(f"News scan error: {e}")
        await asyncio.sleep(30)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,  # must be 1 for WebSocket state
        log_level="info"
    )
