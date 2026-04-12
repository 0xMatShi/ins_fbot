"""
Скрипт для сбора insider-кошельков с polysights.xyz
API: https://www.polysights.xyz/api/insider-finder?batch=N&...
Фильтры: UNIQUE MARKETS 0-5, WC/TX DELTA (DAYS) 0-25
"""

import asyncio
import aiohttp
import csv
import json
import math

API_URL = "https://www.polysights.xyz/api/insider-finder"
CONCURRENCY = 20

# Фильтры из UI (UNIQUE MARKETS: 0-5, WC/TX DELTA: 0-25d)
FILTERS = {
    "uniqueMarketsMin": 0,
    "uniqueMarketsMax": 5,
    "wcTxDeltaMin": 0,
    "wcTxDeltaMax": 25,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.polysights.xyz/insider-finder",
}


def build_url(batch: int, skip_count: bool = True) -> str:
    params = f"batch={batch}&skipCount={'true' if skip_count else 'false'}"
    for k, v in FILTERS.items():
        params += f"&{k}={v}"
    return f"{API_URL}?{params}"


async def fetch_batch(session: aiohttp.ClientSession, batch: int) -> list[dict]:
    url = build_url(batch)
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"  [!] batch={batch} status={resp.status}")
                return []
            data = await resp.json()
            return data.get("data", [])
    except Exception as e:
        print(f"  [!] batch={batch} error: {e}")
        return []


async def get_total_count() -> int:
    async with aiohttp.ClientSession() as session:
        url = build_url(1, skip_count=False)
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            return data.get("totalCount", 0)


async def fetch_all(total_batches: int) -> list[dict]:
    all_records: list[dict] = []
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        async def bounded_fetch(batch: int) -> list[dict]:
            async with semaphore:
                records = await fetch_batch(session, batch)
                if batch % 20 == 0:
                    print(f"  batch {batch}/{total_batches} ({len(records)} records)")
                return records

        tasks = [bounded_fetch(b) for b in range(1, total_batches + 1)]
        results = await asyncio.gather(*tasks)

    for batch_records in results:
        all_records.extend(batch_records)

    return all_records


def aggregate_wallets(records: list[dict]) -> list[dict]:
    """Дедупликация по proxyWallet, берём первую запись для каждого кошелька."""
    wallet_map: dict[str, dict] = {}
    for rec in records:
        wallet = rec.get("proxyWallet", "").lower()
        if not wallet or wallet in wallet_map:
            continue
        wallet_map[wallet] = {
            "proxyWallet": wallet,
            "pseudonym": rec.get("pseudonym", ""),
            "name": rec.get("name", ""),
            "user_profit_loss": rec.get("user_profit_loss", 0),
            "user_volume": rec.get("user_volume", 0),
            "user_volume_traded": rec.get("user_volume_traded", 0),
            "markets_traded": rec.get("markets_traded", 0),
            "total_trades": rec.get("total_trades", 0),
            "walletAge": rec.get("walletAge", ""),
            "active_trader": rec.get("active_trader", False),
            "crypto_t": rec.get("crypto_t", 0),
            "sports_t": rec.get("sports_t", 0),
            "price_t": rec.get("price_t", 0),
        }

    wallets = list(wallet_map.values())
    wallets.sort(key=lambda x: x["user_profit_loss"] or 0, reverse=True)
    return wallets


def save_csv(wallets: list[dict], path: str):
    if not wallets:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(wallets[0].keys()))
        writer.writeheader()
        writer.writerows(wallets)
    print(f"  Saved CSV:  {path}")


def save_json(wallets: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wallets, f, ensure_ascii=False, indent=2)
    print(f"  Saved JSON: {path}")


async def main():
    print("Получаем totalCount с фильтрами...")
    total_count = await get_total_count()
    total_batches = math.ceil(total_count / 100)
    print(f"Всего записей: {total_count} → {total_batches} батчей (concurrency={CONCURRENCY})")
    print(f"Фильтры: uniqueMarkets 0-{FILTERS['uniqueMarketsMax']}, wcTxDelta 0-{FILTERS['wcTxDeltaMax']}d\n")

    records = await fetch_all(total_batches)
    print(f"\nЗагружено записей: {len(records)}")

    wallets = aggregate_wallets(records)
    print(f"Уникальных кошельков: {len(wallets)}\n")

    save_csv(wallets, "insider_wallets.csv")
    save_json(wallets, "insider_wallets.json")

    print("\nТоп-10 по прибыли:")
    print(f"{'Wallet':<44} {'Pseudonym':<25} {'Profit':>12} {'Volume':>12} {'Markets':>8}")
    print("-" * 107)
    for w in wallets[:10]:
        print(f"{w['proxyWallet']:<44} {w['pseudonym']:<25} {w['user_profit_loss']:>12.2f} {w['user_volume']:>12.2f} {w['markets_traded']:>8}")


if __name__ == "__main__":
    asyncio.run(main())
