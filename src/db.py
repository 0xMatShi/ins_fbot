"""
SQLite-слой для хранения транзакций и данных кошельков.

Таблицы:
  trades  — одна строка на транзакцию (ключ: tx_hash)
  wallets — одна строка на кошелёк, обновляется при каждом новом трейде
  users   — chat_id пользователей, запустивших бота
"""

import time
from pathlib import Path

import aiosqlite

DB_PATH = Path("insider.db")


# ─── Схема ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    tx_hash             TEXT PRIMARY KEY,
    proxy_wallet        TEXT NOT NULL,
    timestamp           INTEGER NOT NULL,
    title               TEXT,
    event_slug          TEXT,
    outcome             TEXT,
    side                TEXT,
    size                REAL,
    price               REAL,
    initial_entry       REAL,
    avg_entry_price     REAL,
    market_trades       INTEGER,
    market_volume_traded REAL,
    market_profit_loss  REAL,
    saved_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    proxy_wallet        TEXT PRIMARY KEY,
    pseudonym           TEXT,
    name                TEXT,
    total_trades        INTEGER,
    markets_traded      INTEGER,
    open_positions      INTEGER,
    positions_value     REAL,
    user_profit_loss    REAL,
    user_volume         REAL,
    user_volume_traded  REAL,
    avg_entry_price     REAL,
    wallet_age          TEXT,
    updated_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    chat_id   INTEGER PRIMARY KEY,
    joined_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(proxy_wallet);
CREATE INDEX IF NOT EXISTS idx_trades_ts     ON trades(timestamp DESC);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ─── Запись ───────────────────────────────────────────────────────────────────

async def save_trade(trade: dict) -> None:
    """Сохранить транзакцию + обновить данные кошелька."""
    tx_hash = (trade.get("transactionHash") or f"ts_{trade.get('timestamp', 0)}_{trade.get('proxyWallet','')[:8]}")[:57]
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        # Транзакция (INSERT OR IGNORE — не перезаписываем историю)
        await db.execute("""
            INSERT OR IGNORE INTO trades
                (tx_hash, proxy_wallet, timestamp, title, event_slug,
                 outcome, side, size, price, initial_entry, avg_entry_price,
                 market_trades, market_volume_traded, market_profit_loss, saved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tx_hash,
            trade.get("proxyWallet", ""),
            trade.get("timestamp", 0),
            trade.get("title", ""),
            trade.get("eventSlug", ""),
            trade.get("outcome", ""),
            trade.get("side", ""),
            trade.get("size"),
            trade.get("price"),
            trade.get("initial_entry"),
            trade.get("avg_entry_price"),
            trade.get("market_trades"),
            trade.get("market_volume_traded"),
            trade.get("market_profit_loss"),
            now,
        ))

        # Кошелёк (INSERT OR REPLACE — всегда самые свежие метрики)
        await db.execute("""
            INSERT INTO wallets
                (proxy_wallet, pseudonym, name, total_trades, markets_traded,
                 open_positions, positions_value, user_profit_loss, user_volume,
                 user_volume_traded, avg_entry_price, wallet_age, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(proxy_wallet) DO UPDATE SET
                pseudonym         = excluded.pseudonym,
                name              = excluded.name,
                total_trades      = excluded.total_trades,
                markets_traded    = excluded.markets_traded,
                open_positions    = excluded.open_positions,
                positions_value   = excluded.positions_value,
                user_profit_loss  = excluded.user_profit_loss,
                user_volume       = excluded.user_volume,
                user_volume_traded = excluded.user_volume_traded,
                avg_entry_price   = excluded.avg_entry_price,
                wallet_age        = excluded.wallet_age,
                updated_at        = excluded.updated_at
        """, (
            trade.get("proxyWallet", ""),
            trade.get("pseudonym", ""),
            trade.get("name", ""),
            trade.get("total_trades"),
            trade.get("markets_traded"),
            trade.get("open_positions"),
            trade.get("positions_value"),
            trade.get("user_profit_loss"),
            trade.get("user_volume"),
            trade.get("user_volume_traded"),
            trade.get("avg_entry_price"),
            trade.get("walletAge", ""),
            now,
        ))

        await db.commit()


# ─── Чтение ───────────────────────────────────────────────────────────────────

async def get_trade(tx_hash: str) -> dict | None:
    """Получить данные конкретной транзакции."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE tx_hash = ?", (tx_hash,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_wallet(proxy_wallet: str) -> dict | None:
    """Получить актуальные метрики кошелька."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wallets WHERE proxy_wallet = ?", (proxy_wallet,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_recent_trades(limit: int = 25) -> list[dict]:
    """Последние N транзакций по всем кошелькам."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_wallet_trades(proxy_wallet: str, limit: int = 20) -> list[dict]:
    """Последние N транзакций кошелька (для истории)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE proxy_wallet = ? ORDER BY timestamp DESC LIMIT ?",
            (proxy_wallet, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def add_user(chat_id: int) -> None:
    """Зарегистрировать пользователя (идемпотентно)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (chat_id, joined_at) VALUES (?, ?)",
            (chat_id, int(time.time())),
        )
        await db.commit()


async def get_all_users() -> list[int]:
    """Вернуть все chat_id зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM users") as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def db_stats() -> dict:
    """Статистика БД для /status."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM trades") as cur:
            trades_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM wallets") as cur:
            wallets_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            users_count = (await cur.fetchone())[0]
    return {"trades": trades_count, "wallets": wallets_count, "users": users_count}
