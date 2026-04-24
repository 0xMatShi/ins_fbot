# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Polysights — асинхронный Telegram-бот на Python, который мониторит инсайдерские трейды на Polymarket через API `polysights.xyz` и рассылает уведомления подписанным пользователям каждые 10 минут.

## Running the project

```bash
# Install dependencies
uv sync

# Run the bot
python main.py
```

**Required `.env`:**
```
BOT_TOKEN=your_telegram_bot_token
```

State сохраняется в `state.json` (последний опрошенный timestamp), база данных — `insider.db` (SQLite, создаётся автоматически).

## Architecture

```
main.py              # asyncio.run(main()) — запускает бота и poll_loop параллельно
src/
  bot.py             # вся логика бота (~550 строк)
  db.py              # SQLite-слой (~220 строк)
  fetch_insider_wallets.py  # автономная утилита для batch-экспорта кошельков в CSV/JSON
insider.db           # runtime, в .gitignore
state.json           # runtime, в .gitignore
```

### Поток данных

1. `poll_loop` в `bot.py` раз в `POLL_INTERVAL=600s` вызывает `fetch_latest_trades`
2. `fetch_latest_trades` запрашивает `https://www.polysights.xyz/api/insider-finder` с hardcoded `FILTERS`
3. Новые трейды (дедупликация по `tx_hash`) сохраняются через `save_trade()` в `db.py`
4. `send_trades` рассылает HTML-сообщения всем пользователям из `get_all_users()`
5. Пользователи могут запросить историю `/last25_trades` или детали кошелька через inline-кнопки

### db.py — три таблицы

- `trades` — транзакции (PK: `tx_hash`, индекс по `wallet + timestamp`)
- `wallets` — метрики кошельков (upsert при каждом трейде)
- `users` — Telegram chat_id подписчиков

### Radar Score (в `bot.py: calc_radar_score`)

Алгоритм 0–100, комбинирует 7 факторов с весами:
- Unique Markets, Entry Price, Size, Trade Concentration, Volume Concentration, Loss Rate, WC/TX%

Изменять с осторожностью — это ключевая бизнес-логика.

### Hardcoded constants (bot.py)

```python
API_URL = "https://www.polysights.xyz/api/insider-finder"
POLL_INTERVAL = 10 * 60          # секунды
INITIAL_SEND_COUNT = 25          # трейдов при первом запуске
FILTERS = {
    "uniqueMarketsMin": 0,
    "uniqueMarketsMax": 5,
    "wcTxDeltaMin": 0,
    "wcTxDeltaMax": 25,          # дней
}
```

## Key dependencies

- `aiogram>=3.17` — async Telegram bot framework
- `aiohttp` — HTTP-клиент для API
- `aiosqlite` — async SQLite
- `python-dotenv` — загрузка `.env`

Python версия: 3.14 (`.python-version`). Пакетный менеджер: `uv`.
