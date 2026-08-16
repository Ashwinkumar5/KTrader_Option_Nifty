from __future__ import annotations

import json
import sqlite3
import asyncio
from pathlib import Path
from typing import Optional

from app.domain.models import AnalyticsSnapshot, MarketTick, OptionChainSnapshot
from app.storage.serialization import to_jsonable


_TICKS_TABLE = "ticks"
_CHAIN_TABLE = "chain_snapshots"
_LIVE_CHAIN_TABLE = "live_chain"
_LIVE_ANALYTICS_TABLE = "live_analytics"


def _ensure_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_TICKS_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, received_at TEXT, payload TEXT)"
    )
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_CHAIN_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, underlying TEXT, captured_at TEXT, payload TEXT)"
    )
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_LIVE_CHAIN_TABLE} (underlying TEXT PRIMARY KEY, payload TEXT)"
    )
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_LIVE_ANALYTICS_TABLE} (underlying TEXT PRIMARY KEY, payload TEXT)"
    )
    conn.commit()
    conn.close()


class SqliteTickStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        _ensure_db(self._path)

    async def save_tick(self, tick: MarketTick) -> None:
        payload = json.dumps(to_jsonable(tick), separators=(",", ":"))
        token = tick.token.token
        received = tick.received_at.isoformat()
        await asyncio.to_thread(self._insert, token, received, payload)

    def _insert(self, token: str, received: str, payload: str) -> None:
        conn = sqlite3.connect(self._path)
        cur = conn.cursor()
        cur.execute(f"INSERT INTO {_TICKS_TABLE} (token, received_at, payload) VALUES (?, ?, ?)", (token, received, payload))
        conn.commit()
        conn.close()


class SqliteChainSnapshotStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        _ensure_db(self._path)

    async def save_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        payload = json.dumps(to_jsonable(snapshot), separators=(",", ":"))
        underlying = snapshot.underlying
        captured = snapshot.captured_at.isoformat()
        await asyncio.to_thread(self._insert, underlying, captured, payload)

    def _insert(self, underlying: str, captured: str, payload: str) -> None:
        conn = sqlite3.connect(self._path)
        cur = conn.cursor()
        cur.execute(f"INSERT INTO {_CHAIN_TABLE} (underlying, captured_at, payload) VALUES (?, ?, ?)", (underlying, captured, payload))
        conn.commit()
        conn.close()


class SqliteLiveStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        _ensure_db(self._path)

    async def publish_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        payload = json.dumps(to_jsonable(snapshot), separators=(",", ":"))
        underlying = snapshot.underlying
        await asyncio.to_thread(self._upsert, _LIVE_CHAIN_TABLE, underlying, payload)

    async def publish_analytics_snapshot(self, snapshot: AnalyticsSnapshot) -> None:
        payload = json.dumps(to_jsonable(snapshot), separators=(",", ":"))
        underlying = snapshot.underlying
        await asyncio.to_thread(self._upsert, _LIVE_ANALYTICS_TABLE, underlying, payload)

    def _upsert(self, table: str, underlying: str, payload: str) -> None:
        conn = sqlite3.connect(self._path)
        cur = conn.cursor()
        # try update, otherwise insert
        cur.execute(f"UPDATE {table} SET payload = ? WHERE underlying = ?", (payload, underlying))
        if cur.rowcount == 0:
            cur.execute(f"INSERT INTO {table} (underlying, payload) VALUES (?, ?)", (underlying, payload))
        conn.commit()
        conn.close()
