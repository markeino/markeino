"""
DEX Arbitrage Monitor
Polls ETH/USDC, WBTC/USDC, and ETH/USDT across 5 DEXes via GraphQL / REST APIs.
Alerts when fee-adjusted net spread exceeds threshold.

Note: DEX prices update on-chain per block (~12 s on Ethereum) — no WebSocket needed.
"""

import asyncio
import aiohttp
import csv
import os
import time
from collections import defaultdict
from datetime import datetime

# ─── Pairs ────────────────────────────────────────────────────────────────────
# All routes through USD stablecoins (USDC / USDT / DAI)
PAIRS = [
    "ETH/USDC",
    "WBTC/USDC",
    "ETH/USDT",
]

# ─── DEX Fee Table  (swap fee paid to LPs, %) ────────────────────────────────
DEX_FEE_PCT: dict[str, float] = {
    "uniswap_v3":  0.30,   # 0.05% for ETH/USDC; 0.30% for WBTC/USDC, ETH/USDT
    "pancakeswap": 0.25,   # PancakeSwap v3 Ethereum
    "curve":       0.04,   # tricrypto2 / crypto pools
    "sushiswap":   0.30,   # SushiSwap v2
    "balancer":    0.10,   # Balancer weighted pools (varies; 0.10% common)
}

# Per-pair fee overrides for specific pool tiers
_DEX_PAIR_FEE: dict[str, dict[str, float]] = {
    "uniswap_v3": {
        "ETH/USDC":  0.05,
        "WBTC/USDC": 0.30,
        "ETH/USDT":  0.30,
    },
}

def get_fee(dex: str, pair: str) -> float:
    return _DEX_PAIR_FEE.get(dex, {}).get(pair, DEX_FEE_PCT.get(dex, 0.30))

# ─── Chain mapping ────────────────────────────────────────────────────────────
DEX_CHAIN = {
    "uniswap_v3":  "ethereum",
    "pancakeswap": "ethereum",
    "curve":       "ethereum",
    "sushiswap":   "ethereum",
    "balancer":    "ethereum",
}

# ─── Pool addresses (Ethereum mainnet) ───────────────────────────────────────
# All prices fetched via DexScreener REST API (no API key required).
# DexScreener endpoint: GET /latest/dex/pairs/ethereum/{address}

_DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/pairs/ethereum/{}"

_UNISWAP_POOLS = {
    "ETH/USDC":  "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # USDC/WETH 0.05%
    "WBTC/USDC": "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35",  # WBTC/USDC 0.30%
    "ETH/USDT":  "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36",  # USDT/WETH 0.30%
}

_PANCAKE_POOLS = {
    "ETH/USDC":  "0x1ac1a8feaaea1900c4166deeed0c11cc10669d36",
    "WBTC/USDC": "0xd9e2a1a61b6e61b275cec326465d417e52c1b95c",
    "ETH/USDT":  "0x6ca298d2983ab03aa1da7679389d955a4efee15c",
}

_SUSHI_PAIRS = {
    "ETH/USDC":  "0x397ff1542f962076d0bfe58ea045ffa2d347aca0",
    "WBTC/USDC": "0xceff51756c56ceffca006cd410b03ffc46dd3a58",
    "ETH/USDT":  "0x06da0fd433c1a5d7a4faa01111c044910a184553",
}

# Balancer: use first 20 bytes (contract address) of the 32-byte pool ID
_BALANCER_POOLS = {
    "ETH/USDC":  "0x96646936b91d6b9d7d0c47c496afbf3d6ec7b6f",
    "WBTC/USDC": "0x8a819a4cabd6efcb4e5504fe8679a1abd831dd8f",
    "ETH/USDT":  "0x3e5fa9518ea95c3e533eb377c001702a9aacaa32",
}

# Curve tricrypto2 pool address (USDT/WBTC/WETH)
_CURVE_POOLS = {
    "ETH/USDC":  "0xd51a44d3fae010294c616388b506acda1bfaae46",
    "WBTC/USDC": "0xd51a44d3fae010294c616388b506acda1bfaae46",
    "ETH/USDT":  "0xd51a44d3fae010294c616388b506acda1bfaae46",
}

# ─── Thresholds ───────────────────────────────────────────────────────────────
MIN_SPREAD_PCT  = 0.05    # % gross spread to trigger an alert check
ALERT_COOLDOWN  = 10      # seconds between repeated alerts for the same pair
MIN_TRADE_USDT  = 1_000.0 # minimum USD trade size for profit estimates
POLL_INTERVAL   = 12      # seconds between polls (≈ 1 Ethereum block)
MAX_QUOTE_AGE   = 60.0    # discard quotes older than this (seconds)
STALE_LOG_EVERY = 30.0    # seconds between stale-quote log lines
ALERT_LOG_FILE  = "dex_arb_alerts.csv"

# ─── Alert Log Setup ──────────────────────────────────────────────────────────
_log_file = open(ALERT_LOG_FILE, "a", newline="", buffering=1)
_csv = csv.writer(_log_file)
if os.path.getsize(ALERT_LOG_FILE) == 0:
    _csv.writerow([
        "timestamp", "pair",
        "buy_dex", "buy_price",
        "sell_dex", "sell_price",
        "gross_spread_pct", "fee_buy_pct", "fee_sell_pct",
        "net_spread_pct", "trade_size_usdt", "est_profit_usdt",
    ])

def log_alert(ts, pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, trade_usdt, profit_usdt):
    _csv.writerow([
        ts, pair,
        buy_dex, f"{buy_price:.6f}",
        sell_dex, f"{sell_price:.6f}",
        f"{gross_pct:.4f}", f"{fee_buy:.4f}", f"{fee_sell:.4f}",
        f"{net_pct:.4f}", f"{trade_usdt:.2f}", f"{profit_usdt:.4f}",
    ])

# ─── Shared State ─────────────────────────────────────────────────────────────
# prices[pair][dex] = {"price": float, "ts": float}
prices: dict[str, dict[str, dict]] = defaultdict(dict)
last_alert: dict[str, float]       = defaultdict(float)
last_stale_log: dict[tuple, float] = defaultdict(float)

# ─── Price Fetchers (DexScreener REST API) ────────────────────────────────────

async def _fetch_dexscreener(session: aiohttp.ClientSession,
                              pool_address: str) -> float | None:
    url = _DEXSCREENER_URL.format(pool_address)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    price_str = pairs[0].get("priceUsd")
    if price_str is None:
        return None
    return float(price_str)


async def fetch_uniswap_v3(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _UNISWAP_POOLS.get(pair)
    return await _fetch_dexscreener(session, addr) if addr else None


async def fetch_pancakeswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _PANCAKE_POOLS.get(pair)
    return await _fetch_dexscreener(session, addr) if addr else None


async def fetch_sushiswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _SUSHI_PAIRS.get(pair)
    return await _fetch_dexscreener(session, addr) if addr else None


async def fetch_balancer(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _BALANCER_POOLS.get(pair)
    return await _fetch_dexscreener(session, addr) if addr else None


async def fetch_curve(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _CURVE_POOLS.get(pair)
    return await _fetch_dexscreener(session, addr) if addr else None


FETCHERS = {
    "uniswap_v3":  fetch_uniswap_v3,
    "pancakeswap": fetch_pancakeswap,
    "curve":       fetch_curve,
    "sushiswap":   fetch_sushiswap,
    "balancer":    fetch_balancer,
}

# ─── Arbitrage Detection ──────────────────────────────────────────────────────
def check_arbitrage(pair: str) -> None:
    data = prices[pair]
    if len(data) < 2:
        return

    now   = time.time()
    fresh: dict[str, float] = {}

    for dex, v in data.items():
        age = now - v.get("ts", 0)
        if age <= MAX_QUOTE_AGE:
            fresh[dex] = v["price"]
        else:
            key = (dex, pair)
            if now - last_stale_log[key] >= STALE_LOG_EVERY:
                print(f"  [stale] {dex}/{pair}  age={age:.1f}s — skipped")
                last_stale_log[key] = now

    if len(fresh) < 2:
        return

    min_dex = min(fresh, key=fresh.get)   # cheapest — BUY here
    max_dex = max(fresh, key=fresh.get)   # most expensive — SELL here
    if min_dex == max_dex:
        return

    buy_price  = fresh[min_dex]
    sell_price = fresh[max_dex]
    gross_pct  = (sell_price - buy_price) / buy_price * 100

    if gross_pct < MIN_SPREAD_PCT:
        return

    fee_buy  = get_fee(min_dex, pair)
    fee_sell = get_fee(max_dex, pair)
    net_pct  = gross_pct - fee_buy - fee_sell

    qty         = MIN_TRADE_USDT / buy_price
    proceeds    = qty * sell_price * (1 - fee_sell / 100)
    cost        = MIN_TRADE_USDT  * (1 + fee_buy  / 100)
    profit_usdt = proceeds - cost

    mono_now = time.monotonic()
    if mono_now - last_alert[pair] < ALERT_COOLDOWN:
        return
    last_alert[pair] = mono_now

    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    print(f"\n{'='*64}")
    print(f"  ARB ALERT  {pair}  [{ts}]")
    print(f"{'='*64}")
    print(f"  BUY  on {min_dex:<14}  price = {buy_price:>12.4f}  fee = {fee_buy:.3f}%")
    print(f"  SELL on {max_dex:<14}  price = {sell_price:>12.4f}  fee = {fee_sell:.3f}%")
    print(f"  Gross spread : {gross_pct:>8.4f}%")
    print(f"  Total fees   : {fee_buy + fee_sell:>8.4f}%")
    print(f"  Net spread   : {net_pct:>8.4f}%  {'✓ PROFITABLE' if net_pct > 0 else '✗ fee-negative'}")
    print(f"  Est. profit  : ${profit_usdt:>8.4f}  on ${MIN_TRADE_USDT:,.0f} trade")
    print(f"{'='*64}")

    print(f"\n  Price snapshot — {pair}:")
    print(f"  {'DEX':<16} {'Price (USD)':>14} {'Age(s)':>8}")
    print(f"  {'-'*42}")
    for dex, v in sorted(data.items()):
        age = f"{now - v['ts']:.1f}" if v.get("ts") else "—"
        print(f"  {dex:<16} {v['price']:>14.4f} {age:>8}")
    print()

    log_alert(ts, pair, min_dex, buy_price, max_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, MIN_TRADE_USDT, profit_usdt)

# ─── Polling Loop Per DEX ─────────────────────────────────────────────────────
async def poll_dex(dex_id: str, pair: str) -> None:
    fetcher     = FETCHERS[dex_id]
    retry_delay = 2
    failures    = 0
    last_err    = ""

    print(f"[{dex_id}] Polling {pair}  (chain: {DEX_CHAIN.get(dex_id)})")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                price = await fetcher(session, pair)
                if price and price > 0:
                    prices[pair][dex_id] = {"price": price, "ts": time.time()}
                    check_arbitrage(pair)
                    retry_delay = 2
                    failures    = 0
                    last_err    = ""
                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:120]}"
                failures += 1
                if failures >= 8:
                    print(f"[{dex_id}/{pair}] Giving up after 8 failures. Last: {err}")
                    return
                if err != last_err:
                    print(f"[{dex_id}/{pair}] {err}  (retry {failures}/8 in {retry_delay}s)")
                    last_err = err
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

# ─── Periodic Status Table ────────────────────────────────────────────────────
async def status_printer() -> None:
    await asyncio.sleep(20)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        print(f"\n{'─'*64}")
        print(f"  PRICE SNAPSHOT  {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'─'*64}")
        for pair in PAIRS:
            data = prices.get(pair, {})
            if not data:
                continue
            print(f"\n  {pair}")
            print(f"  {'DEX':<16} {'Price (USD)':>14} {'Age(s)':>8}")
            print(f"  {'-'*42}")
            for dex, v in sorted(data.items()):
                age = f"{now - v['ts']:.1f}" if v.get("ts") else "—"
                print(f"  {dex:<16} {v['price']:>14.4f} {age:>8}")
        print(f"\n{'─'*64}\n")

# ─── Entry Point ──────────────────────────────────────────────────────────────
async def main() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║              DEX Arbitrage Monitor                       ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Pairs      : {', '.join(PAIRS)}")
    print(f"  DEXes      : {', '.join(FETCHERS)}")
    print(f"  Threshold  : {MIN_SPREAD_PCT}% gross spread")
    print(f"  Min trade  : ${MIN_TRADE_USDT:,.0f} USD")
    print(f"  Poll every : {POLL_INTERVAL}s  (≈ 1 Ethereum block)")
    print(f"  Alert log  : {ALERT_LOG_FILE}")
    print()

    tasks = [
        poll_dex(dex_id, pair)
        for pair in PAIRS
        for dex_id in FETCHERS
    ]
    tasks.append(status_printer())
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down. Goodbye.")
    finally:
        _log_file.close()
