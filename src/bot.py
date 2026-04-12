"""
Telegram-бот: полинг polysights.xyz/insider-finder каждые 10 минут.
Фильтры: UNIQUE MARKETS 0–5, WC/TX DELTA 0–25d

.env:
  BOT_TOKEN  — токен от @BotFather
  CHAT_IDS   — chat_id через запятую
"""

import asyncio
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from src import db

load_dotenv()

BOT_TOKEN     = os.environ["BOT_TOKEN"]
POLL_INTERVAL      = 10 * 60
INITIAL_SEND_COUNT = 25
INITIAL_MAX_AGE_S  = 24 * 3600   # не слать трейды старше 24 часов при первом запуске
STATE_FILE         = Path("state.json")

API_URL = "https://www.polysights.xyz/api/insider-finder"
FILTERS = {
    "uniqueMarketsMin": 0,
    "uniqueMarketsMax": 5,
    "wcTxDeltaMin":     0,
    "wcTxDeltaMax":     25,
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.polysights.xyz/insider-finder",
}


# ─── Radar Score ──────────────────────────────────────────────────────────────

def calc_radar_score(
    trade_row: dict,      # данные транзакции (из API или из таблицы trades)
    wallet_row: dict,     # актуальные метрики кошелька (из таблицы wallets)
) -> tuple[int, dict[str, int]]:
    """
    Формула из defc54a8401b14d2.js (transformTradeToCardData).
    trade_row  — поля конкретного трейда
    wallet_row — свежие wallet-метрики (могут быть новее трейда)
    """
    price      = trade_row.get("price") or 0.0
    size       = trade_row.get("size") or 0.0
    c          = size * price                           # trade size in $

    total_tr   = wallet_row.get("total_trades") or 0
    market_tr  = trade_row.get("market_trades") or 0
    u          = (market_tr / total_tr * 100) if total_tr > 0 else 0.0

    user_vol   = wallet_row.get("user_volume") or 0.0
    mkt_vol    = trade_row.get("market_volume_traded") or 0.0
    h          = (mkt_vol / user_vol * 100) if user_vol > 0 else 0.0

    now_s      = datetime.now(timezone.utc).timestamp()
    wallet_age_str = wallet_row.get("wallet_age") or trade_row.get("walletAge", "")
    try:
        wallet_age_days = (now_s - datetime.fromisoformat(
            wallet_age_str.replace("Z", "+00:00")
        ).timestamp()) / 86_400
    except Exception:
        wallet_age_days = 0.0

    ts          = trade_row.get("timestamp") or 0
    tx_age_days = (now_s - ts) / 86_400
    b           = (tx_age_days / wallet_age_days * 100) if wallet_age_days > 0 else 0.0

    v           = 0.4 if (total_tr < 2 and u >= 100 and h >= 100) else 1.0

    init_entry  = trade_row.get("initial_entry") or trade_row.get("init_price") or 0.0
    pnl         = wallet_row.get("user_profit_loss") or 0.0
    mkts        = wallet_row.get("markets_traded") or 0

    factors = [
        ("Unique Mkts.", (5 if mkts==1 else 4 if mkts<=2 else 3 if mkts<=5 else 2 if mkts<=10 else 1) / 5),
        ("Entry",        (5 if init_entry<=.20 else 4 if init_entry<=.35 else 3 if init_entry<=.55 else 2 if init_entry<=.70 else 1 if init_entry<=.75 else .5) / 5),
        ("Size",         (5 if c>=10_000 else 4 if c>=5_000 else 3 if c>=2_500 else 2 if c>=1_750 else .5) / 5),
        ("Trade Conc.",  (5 if u>=90 else 4 if u>=50 else 3 if u>=25 else 2 if u>=10 else 1) / 5 * v),
        ("Vol. Conc.",   (5 if h>=90 else 4 if h>=50 else 3 if h>=25 else 2 if h>=10 else 1) / 5 * v),
        ("Loss Rate",    (5 if pnl>=1_000 else 4 if pnl>=0 else 3 if pnl>=-100 else 2 if pnl>=-500 else 1) / 5),
        ("WC/TX%",       (1 if b<=2 else 2 if b<=10 else 3 if b<=50 else 4 if b<=90 else 5) / 5),
    ]
    score = sum(v for _, v in factors) / len(factors) * 100
    return round(score), {name: round(v * 5) for name, v in factors}


# ─── Форматирование ───────────────────────────────────────────────────────────

def fmt_usd(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000: return f"{sign}${a/1_000_000:.2f}M"
    if a >= 1_000:     return f"{sign}${a/1_000:.1f}K"
    return f"{sign}${a:.0f}"


def fmt_price(p: float | None) -> str:
    if p is None: return "N/A"
    c = p * 100
    return f"{c:.1f}¢" if c < 100 else f"${p:.2f}"


def fmt_wallet_age(s: str) -> str:
    if not s: return "N/A"
    try:
        delta_s  = (datetime.now(timezone.utc) - datetime.fromisoformat(s.replace("Z", "+00:00"))).total_seconds()
        days, h  = divmod(int(delta_s / 3600), 24)
        if days:  return f"{days}d {h}h" if h else f"{days}d"
        if h:     return f"{h}h"
        return f"{int(delta_s/60)}m"
    except Exception:
        return s


def fmt_ago(ts: int) -> str:
    if not ts: return "N/A"
    d = datetime.now(timezone.utc).timestamp() - ts
    if d < 3600:   return f"{int(d/60)}m ago"
    if d < 86400:  return f"{int(d/3600)}h ago"
    return f"{int(d/86400)}d ago"


def wc_tx_pct(trade_row: dict, wallet_row: dict) -> str:
    now_s = datetime.now(timezone.utc).timestamp()
    try:
        wa_str  = wallet_row.get("wallet_age") or trade_row.get("walletAge", "")
        wa_days = (now_s - datetime.fromisoformat(wa_str.replace("Z","+00:00")).timestamp()) / 86_400
        tx_days = (now_s - (trade_row.get("timestamp") or 0)) / 86_400
        return f"{tx_days / wa_days * 100:.2f}%" if wa_days > 0 else "N/A"
    except Exception:
        return "N/A"


def polymarket_url(slug: str) -> str:
    return f"https://polymarket.com/event/{slug}"

def polygonscan_tx(tx: str) -> str:
    return f"https://polygonscan.com/tx/{tx}"

def polygonscan_addr(addr: str) -> str:
    return f"https://polygonscan.com/address/{addr}"

def polymarket_profile(addr: str) -> str:
    return f"https://polymarket.com/profile/{addr}?via=Polysights"


# ─── Тексты сообщений ────────────────────────────────────────────────────────

def build_trade_text(trade: dict, wallet: dict) -> str:
    """Краткая карточка транзакции (использует свежие wallet-метрики)."""
    wallet     = wallet or {}
    price      = trade.get("price") or 0.0
    size       = trade.get("size") or 0.0
    size_usd   = size * price
    outcome    = html.escape(trade.get("outcome") or "N/A")
    side       = trade.get("side", "BUY")
    mkt_vol    = trade.get("market_volume_traded") or 0.0
    market_tr  = trade.get("market_trades") or 0
    tx_hash    = trade.get("tx_hash") or trade.get("transactionHash", "")
    event_slug = trade.get("event_slug") or trade.get("eventSlug", "")
    title      = html.escape(trade.get("title") or "Unknown market")
    avg_price  = trade.get("avg_entry_price") or 0.0

    total_tr   = wallet.get("total_trades") or trade.get("total_trades") or 0
    open_pos   = wallet.get("open_positions") or trade.get("open_positions") or 0
    mkts       = wallet.get("markets_traded") or trade.get("markets_traded") or 0
    user_vol   = wallet.get("user_volume") or trade.get("user_volume") or 0.0
    wa_str     = wallet.get("wallet_age") or trade.get("walletAge", "")

    score, _   = calc_radar_score(trade, wallet or trade)
    wallet_age = fmt_wallet_age(wa_str)
    trade_conc = f"{market_tr / total_tr * 100:.1f}%" if total_tr else "N/A"
    vol_conc   = f"{mkt_vol / user_vol * 100:.2f}%" if user_vol else "N/A"
    wc_tx      = wc_tx_pct(trade, wallet or trade)
    impact     = f"{size_usd / mkt_vol * 100:.3f}%" if mkt_vol and size_usd else "N/A"
    side_e     = "🟢" if side == "BUY" else "🔴"
    mkt_link   = f'<a href="{polymarket_url(event_slug)}">{title}</a>' if event_slug else title
    poly_link  = f'<a href="{polygonscan_tx(tx_hash)}">Transaction on Polygonscan</a>' if tx_hash else "Polygonscan"
    raw_name   = wallet.get("name") or trade.get("name") or ""
    proxy_addr = trade.get('proxy_wallet') or trade.get('proxyWallet', '')
    name_line  = f"👤 <b>Name:</b> {html.escape(raw_name)}" if raw_name else None
    wallet_line = f"🔑 <b>Wallet:</b> <code>{proxy_addr}</code>"

    header = [name_line, wallet_line] if name_line else [wallet_line]

    return "\n".join([
        f"{side_e} <b>Market:</b> {mkt_link}",
        "",
        *header,
        f"🎯 <b>Radar Score:</b> {score}/100",
        f"💰 <b>Size:</b> {fmt_usd(size_usd)} | Shares: {size:,.0f}",
        f"{side_e} <b>Buy:</b> {outcome} | Price: {fmt_price(price)} | Avg: {fmt_price(avg_price)}",
        "",
        f"📝 <b>Total Trades:</b> {total_tr}",
        f"📊 <b>Open Positions:</b> {open_pos}",
        f"🌐 <b>Unique Markets:</b> {mkts}",
        f"🔀 <b>Trade Conc.:</b> {trade_conc}  |  <b>Vol. Conc.:</b> {vol_conc}",
        f"⏱ <b>WC / TX %:</b> {wc_tx}",
        f"🕐 <b>Wallet Age:</b> {wallet_age}",
        "",
        f"🏛 <b>Market Volume:</b> {fmt_usd(mkt_vol)}",
        f"🔄 <b>Market Trades:</b> {market_tr}",
        f"📈 <b>Trade Impact:</b> {impact}",
        "",
        f"🔗 {poly_link}",
    ])


def build_wallet_text(trade: dict, wallet: dict) -> str:
    """Полная карточка кошелька: trade details из конкретного трейда + свежие user metrics."""
    price      = trade.get("price") or 0.0
    size       = trade.get("size") or 0.0
    size_usd   = size * price
    init_price = trade.get("initial_entry") or 0.0
    avg_price  = trade.get("avg_entry_price") or 0.0
    outcome    = html.escape(trade.get("outcome") or "N/A")
    side       = trade.get("side", "BUY")
    mkt_vol    = trade.get("market_volume_traded") or 0.0
    mkt_pnl    = trade.get("market_profit_loss") or 0.0
    market_tr  = trade.get("market_trades") or 0
    tx_hash    = trade.get("tx_hash") or trade.get("transactionHash", "")
    event_slug = trade.get("event_slug") or trade.get("eventSlug", "")
    title      = html.escape(trade.get("title") or "Unknown market")
    proxy_w    = trade.get("proxy_wallet") or trade.get("proxyWallet", "")
    ts         = trade.get("timestamp") or 0

    # Актуальные метрики кошелька
    name       = html.escape(wallet.get("name") or trade.get("name") or "")
    total_tr   = wallet.get("total_trades") or 0
    mkts       = wallet.get("markets_traded") or 0
    open_pos   = wallet.get("open_positions") or 0
    op_value   = wallet.get("positions_value") or 0.0
    pnl        = wallet.get("user_profit_loss") or 0.0
    user_vol   = wallet.get("user_volume") or 0.0
    user_vol_t = wallet.get("user_volume_traded") or 0.0
    avg_e      = wallet.get("avg_entry_price") or avg_price
    wa_str     = wallet.get("wallet_age") or trade.get("walletAge", "")

    score, factors = calc_radar_score(trade, wallet)
    wallet_age = fmt_wallet_age(wa_str)
    time_ago   = fmt_ago(ts)
    trade_conc = f"{market_tr / total_tr * 100:.1f}%" if total_tr else "N/A"
    vol_conc   = f"{mkt_vol / user_vol * 100:.2f}%" if user_vol else "N/A"
    wc_tx      = wc_tx_pct(trade, wallet)
    total_vol  = fmt_usd(user_vol_t * avg_e)

    side_e     = "🟢" if side == "BUY" else "🔴"
    mkt_link   = f'<a href="{polymarket_url(event_slug)}">{title}</a>' if event_slug else title

    radar_lines = "\n".join(
        f"  {n}: {'★' * v}{'☆' * (5-v)} ({v}/5)"
        for n, v in factors.items()
    )

    return "\n".join([
        f"👤 <b>{name}</b>",
        f"<code>{proxy_w}</code>",
        f"💼 <b>Total PnL:</b> {fmt_usd(pnl)}  ·  ⏱ {time_ago}",
        "",
        f"{side_e} <b>{outcome}</b>  {fmt_price(price)}  —  {mkt_link}",
        "",
        "━━━━━━ 🎯 RADAR SCORE ━━━━━━",
        f"<b>{score}/100</b>",
        radar_lines,
        "",
        "━━━━━━ 📋 TRADE DETAILS ━━━━━━",
        f"SIZE              {fmt_usd(size_usd)}",
        f"BUY PRICE         {fmt_price(price)}",
        f"INIT. PRICE       {fmt_price(init_price)}",
        f"AVG. PRICE        {fmt_price(avg_price)}",
        f"MARKET TRADES     {market_tr}",
        f"MARKET VOLUME     {fmt_usd(mkt_vol)}",
        f"VOLUME CONC.      {vol_conc}",
        f"MARKET PNL        {fmt_usd(mkt_pnl)}",
        "",
        "━━━━━━ 👤 USER METRICS ━━━━━━",
        f"TOTAL TRADES      {total_tr}",
        f"UNIQUE MARKETS    {mkts}",
        f"OPEN POSITIONS    {open_pos}",
        f"AVG. O.P. VALUE   {fmt_usd(op_value)}",
        f"TOTAL VOLUME      {total_vol}",
        f"TOTAL PNL         {fmt_usd(pnl)}",
        f"WC / TX %         {wc_tx}",
        f"TRADE CONC.       {trade_conc}",
        f"WALLET AGE        {wallet_age}",
        "",
        f'🔗 <a href="{polygonscan_tx(tx_hash)}">TX on Polygonscan</a>',
        f'🔗 <a href="{polymarket_profile(proxy_w)}">Profile on Polymarket</a>',
        f'🔗 <a href="{polygonscan_addr(proxy_w)}">Address on Polygonscan</a>',
    ])


# ─── Клавиатуры ──────────────────────────────────────────────────────────────

def trade_kb(tx_hash: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👁 Посмотреть кошелек", callback_data=f"wallet:{tx_hash[:57]}")
    ]])


def wallet_kb(tx_hash: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Назад", callback_data=f"back:{tx_hash[:57]}")
    ]])


# ─── Состояние ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_timestamp": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── API ─────────────────────────────────────────────────────────────────────

async def fetch_latest_trades(session: aiohttp.ClientSession) -> list[dict]:
    params = "batch=1&skipCount=true"
    for k, v in FILTERS.items():
        params += f"&{k}={v}"
    url = f"{API_URL}?{params}"
    print(f"[fetch] GET {url}")
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
        print(f"[fetch] status={r.status}")
        data = await r.json()
        trades = data.get("data", [])
        print(f"[fetch] получено трейдов: {len(trades)}")
        return trades


# ─── Отправка ────────────────────────────────────────────────────────────────

def tx_key(trade: dict) -> str:
    tx = trade.get("transactionHash", "")
    return (tx if tx else f"ts_{trade.get('timestamp',0)}")[:57]


async def send_trades(bot: Bot, trades: list[dict]) -> None:
    # Сохраняем в БД всегда, независимо от наличия подписчиков
    for trade in trades:
        await db.save_trade(trade)
    print(f"[send] Сохранено в БД: {len(trades)} трейдов")

    chat_ids = await db.get_all_users()
    if not chat_ids:
        print("[send] Нет подписчиков — рассылка пропущена")
        return

    print(f"[send] Рассылка {len(trades)} трейдов → {len(chat_ids)} подписчик(ам)")
    for trade in trades:
        key    = tx_key(trade)
        wallet = await db.get_wallet(trade.get("proxyWallet", ""))
        text   = build_trade_text(trade, wallet or trade)
        kb     = trade_kb(key)

        for chat_id in chat_ids:
            try:
                await bot.send_message(
                    chat_id, text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception as e:
                print(f"[send] chat_id={chat_id}: {e}")
        await asyncio.sleep(0.3)


# ─── Полинг ──────────────────────────────────────────────────────────────────

async def poll_loop(bot: Bot) -> None:
    state        = load_state()
    is_first_run = state["last_timestamp"] == 0
    print(f"[poll] Запуск. first_run={is_first_run}, last_timestamp={state['last_timestamp']}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                trades = await fetch_latest_trades(session)

                if is_first_run:
                    cutoff  = int(datetime.now(timezone.utc).timestamp()) - INITIAL_MAX_AGE_S
                    fresh   = [t for t in trades if (t.get("timestamp") or 0) >= cutoff]
                    to_send = sorted(fresh[:INITIAL_SEND_COUNT], key=lambda t: t.get("timestamp") or 0)
                    print(f"[poll] Первый запуск — {len(to_send)} свежих транзакций (не старше {INITIAL_MAX_AGE_S//3600}ч)")
                    await send_trades(bot, to_send)
                    is_first_run = False
                else:
                    new = sorted(
                        [t for t in trades if (t.get("timestamp") or 0) > state["last_timestamp"]],
                        key=lambda t: t.get("timestamp") or 0,
                    )
                    if new:
                        print(f"[poll] {len(new)} новых транзакций")
                        await send_trades(bot, new)
                    else:
                        print("[poll] Нет новых транзакций")

                if trades:
                    new_ts = max(t.get("timestamp") or 0 for t in trades)
                    if new_ts > state["last_timestamp"]:
                        state["last_timestamp"] = new_ts
                        print(f"[poll] last_timestamp обновлён → {new_ts}")
                save_state(state)

            except Exception as e:
                print(f"[poll] Ошибка: {e}")

            await asyncio.sleep(POLL_INTERVAL)


# ─── Handlers ────────────────────────────────────────────────────────────────

dp = Dispatcher()


def user_tag(user) -> str:
    name = f"@{user.username}" if user.username else user.full_name
    return f"{name} (id={user.id})"


@dp.callback_query(F.data.startswith("wallet:"))
async def cb_wallet(call: CallbackQuery) -> None:
    tx_hash = call.data.split(":", 1)[1]
    print(f"[btn] {user_tag(call.from_user)} → Посмотреть кошелек tx={tx_hash[:16]}...")
    trade = await db.get_trade(tx_hash)
    if not trade:
        print(f"[btn] tx={tx_hash[:16]}... не найден в БД")
        await call.answer("Транзакция не найдена в БД", show_alert=True)
        return
    wallet = await db.get_wallet(trade["proxy_wallet"])
    if not wallet:
        print(f"[btn] кошелёк {trade['proxy_wallet'][:16]}... не найден в БД")
        await call.answer("Данные кошелька не найдены", show_alert=True)
        return
    await call.message.edit_text(
        build_wallet_text(trade, wallet),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=wallet_kb(tx_hash),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(call: CallbackQuery) -> None:
    tx_hash = call.data.split(":", 1)[1]
    print(f"[btn] {user_tag(call.from_user)} → Назад tx={tx_hash[:16]}...")
    trade = await db.get_trade(tx_hash)
    if not trade:
        print(f"[btn] tx={tx_hash[:16]}... не найден в БД")
        await call.answer("Транзакция не найдена в БД", show_alert=True)
        return
    wallet = await db.get_wallet(trade["proxy_wallet"])
    await call.message.edit_text(
        build_trade_text(trade, wallet or trade),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=trade_kb(tx_hash),
    )
    await call.answer()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    print(f"[cmd] {user_tag(message.from_user)} → /start")
    await db.add_user(message.chat.id)
    await message.answer(
        "👋 <b>Polysights Insider Bot</b>\n\n"
        "Слежу за новыми трейдами на <b>insider-finder</b> каждые 10 минут.\n"
        "Фильтры: <b>Unique Markets 0–5</b>, <b>WC/TX Delta 0–25d</b>\n\n"
        "Используй /last25_trades чтобы посмотреть последние трейды из базы.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("last25_trades"))
async def cmd_last25_trades(message: Message) -> None:
    print(f"[cmd] {user_tag(message.from_user)} → /last25_trades")
    trades = await db.get_recent_trades(25)
    if not trades:
        await message.answer("В базе пока нет трейдов.")
        return
    print(f"[cmd] /last25_trades → отправляем {len(trades)} трейдов")
    await message.answer(f"Последние {len(trades)} трейдов из базы:")
    for trade in reversed(trades):  # от старых к новым
        wallet = await db.get_wallet(trade["proxy_wallet"])
        text   = build_trade_text(trade, wallet or trade)
        key    = trade["tx_hash"]
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=trade_kb(key),
        )
        await asyncio.sleep(0.2)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    await db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start",         description="Подписаться на уведомления"),
        BotCommand(command="last25_trades", description="Последние 25 трейдов из базы"),
    ])
    print("[main] Бот запущен")
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        poll_loop(bot),
    )
