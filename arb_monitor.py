"""
DEX Arbitrage Monitor — Uniswap V4 / V3
========================================
Monitors ETH pairs across Uniswap V4 (primary) and V3 (comparison/fallback)
via the DexScreener REST API.

Merged from dex_arb_v4.py with the following updates:
  • Updated pairs: ETH/USDC, ETH/USDT, ETH/DAI, ETH/WBTC, ETH/LINK,
                   ETH/UNI, ETH/AAVE, ETH/LDO, ETH/PEPE, ETH/MKR
  • Focused on V4 vs V3 (cross-version arb) — sushiswap/balancer/curve removed
  • Fixed zero-price bug: triangular arb now skips any pair with price ≤ 0
  • Fixed WBTC USD-price inversion (priceUsd fallback when priceNative returned)
  • Added per-pair V4 pool address discovery with higher-quality filtering

Ethereum block time ≈ 12 s → poll interval matches one block.
"""

import asyncio
import aiohttp
import csv
import os
import time
from collections import defaultdict
from datetime import datetime
from itertools import combinations

# ─── Pairs ────────────────────────────────────────────────────────────────────
PAIRS = [
    "ETH/USDC",
    "ETH/USDT",
    "ETH/DAI",
    "ETH/WBTC",
    "ETH/LINK",
    "ETH/UNI",
    "ETH/AAVE",
    "ETH/LDO",
    "ETH/PEPE",
    "ETH/MKR",
]

# ─── Token addresses (Ethereum mainnet) ───────────────────────────────────────
TOKEN_ADDRESSES: dict[str, str] = {
    "WETH":  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT":  "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI":   "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "WBTC":  "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "LINK":  "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "UNI":   "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE":  "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "LDO":   "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",
    "PEPE":  "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
    "MKR":   "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
}

# ─── DEX Definitions ──────────────────────────────────────────────────────────
#
# Uniswap V4 uses a singleton PoolManager — pools live inside the contract
# rather than being separate ERC-20 pairs. DexScreener indexes V4 pools and
# returns them via its standard pairs API just like V3 pools.
#
# V4 PoolManager (Ethereum mainnet): 0x000000000004444c5dc75cB358380D2e3dE08A90
# V4 fee tiers (bps): 100 (0.01%), 500 (0.05%), 3000 (0.30%), 10000 (1.00%)

_DEXSCREENER_ID_MAP: dict[str, list[str]] = {
    "uniswap_v4": ["uniswap_v4", "uniswap-v4"],
    "uniswap_v3": ["uniswap_v3", "uniswap-v3"],
}

DEX_FEE_PCT: dict[str, float] = {
    "uniswap_v4": 0.30,
    "uniswap_v3": 0.30,
}

_DEX_PAIR_FEE: dict[str, dict[str, float]] = {
    "uniswap_v4": {
        "ETH/USDC": 0.05,
        "ETH/USDT": 0.05,
        "ETH/DAI":  0.05,
    },
    "uniswap_v3": {
        "ETH/USDC": 0.05,
        "ETH/USDT": 0.05,
        "ETH/DAI":  0.05,
    },
}

def get_fee(dex: str, pair: str) -> float:
    return _DEX_PAIR_FEE.get(dex, {}).get(pair, DEX_FEE_PCT.get(dex, 0.30))

# ─── Known Pool Addresses (Ethereum mainnet, V3) ──────────────────────────────
# V4 pools are discovered dynamically via DexScreener search.
_KNOWN_POOLS: dict[str, dict[str, str]] = {
    "uniswap_v3": {
        "ETH/USDC": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # 0.05%
        "ETH/USDT": "0x11b815efb8f581194ae79006d24e0d814b7697f6",  # 0.05%
        "ETH/DAI":  "0x60594a405d53811d3bc4766596efd80fd545a270",  # 0.05%
        "ETH/WBTC": "0xcbcdf9626bc03e24f779434178a73a0b4bad62ed",  # 0.30%
        "ETH/LINK": "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8",  # 0.30%
        "ETH/UNI":  "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801",  # 0.30%
        "ETH/AAVE": "0x5aB53EE1d50eeF2C1DD3d5402789cd27bB52c1bB",  # 0.30%
        "ETH/PEPE": "0x11950d141ecb863f01007add7d1a342041227b58",  # 1.00%
        "ETH/MKR":  "0xe8c6c9227491c0a8156a0106a0204d881bb7e531",  # 0.30%
    },
}

# ─── DexScreener URLs ─────────────────────────────────────────────────────────
_DS_PAIRS_URL  = "https://api.dexscreener.com/latest/dex/pairs/ethereum/{}"
_DS_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={}"

# ─── Thresholds & Config ──────────────────────────────────────────────────────
MIN_SPREAD_PCT     = 0.05    # minimum gross spread to flag cross-version arb (%)
TRI_ARB_THRESHOLD  = 0.10    # minimum gross spread for triangular arb (%)
ALERT_COOLDOWN     = 10      # seconds between alerts for same signal
MIN_TRADE_USDT     = 1_000.0
POLL_INTERVAL      = 12      # seconds (≈ 1 Ethereum block)
MAX_QUOTE_AGE      = 60.0    # discard quotes older than this (seconds)
STALE_LOG_EVERY    = 30.0    # rate-limit stale-quote warnings
MIN_POOL_LIQUIDITY = 50_000  # USD — skip pools below this
ALERT_LOG_FILE     = "dex_arb_v4_alerts.csv"

# ─── Alert Log ────────────────────────────────────────────────────────────────
_log_file = open(ALERT_LOG_FILE, "a", newline="", buffering=1)
_csv = csv.writer(_log_file)
if os.path.getsize(ALERT_LOG_FILE) == 0:
    _csv.writerow([
        "timestamp", "arb_type", "pair",
        "buy_dex", "buy_price",
        "sell_dex", "sell_price",
        "gross_spread_pct", "fee_buy_pct", "fee_sell_pct",
        "net_spread_pct", "trade_size_usdt", "est_profit_usdt",
    ])

def log_alert(ts, arb_type, pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, trade_usdt, profit_usdt):
    _csv.writerow([
        ts, arb_type, pair,
        buy_dex, f"{buy_price:.6f}",
        sell_dex, f"{sell_price:.6f}",
        f"{gross_pct:.4f}", f"{fee_buy:.4f}", f"{fee_sell:.4f}",
        f"{net_pct:.4f}", f"{trade_usdt:.2f}", f"{profit_usdt:.4f}",
    ])

# ─── Shared State ─────────────────────────────────────────────────────────────
prices:         dict[str, dict[str, dict]]   = defaultdict(dict)
last_alert:     dict[str, float]             = defaultdict(float)
last_stale_log: dict[tuple, float]           = defaultdict(float)

_pool_cache:      dict[tuple[str, str], str | None] = {}
_pool_cache_lock  = asyncio.Lock()

# ─── Pool Discovery ───────────────────────────────────────────────────────────

def _normalize_symbol(s: str) -> str:
    return "ETH" if s.upper() == "WETH" else s.upper()

def _dex_matches(dex_id_str: str, logical_dex: str) -> bool:
    dex_id_lower = dex_id_str.lower()
    return any(slug in dex_id_lower for slug in _DEXSCREENER_ID_MAP.get(logical_dex, [logical_dex]))

def _pair_matches(pair_data: dict, base: str, quote: str) -> bool:
    bt = _normalize_symbol(pair_data.get("baseToken",  {}).get("symbol", ""))
    qt = _normalize_symbol(pair_data.get("quoteToken", {}).get("symbol", ""))
    b  = _normalize_symbol(base)
    q  = _normalize_symbol(quote)
    return (bt == b and qt == q) or (bt == q and qt == b)

async def _discover_pool(
    session:  aiohttp.ClientSession,
    dex_id:   str,
    pair:     str,
) -> str | None:
    """
    Search DexScreener for the highest-liquidity pool matching `pair` on `dex_id`.
    Returns the pool address string or None if not found. Results are cached.
    """
    cache_key = (dex_id, pair)
    async with _pool_cache_lock:
        if cache_key in _pool_cache:
            return _pool_cache[cache_key]

    base, quote = pair.split("/")
    queries = {f"{base} {quote}", f"W{base} {quote}" if base == "ETH" else f"{base} {quote}"}

    best_address:   str | None = None
    best_liquidity: float      = 0.0

    timeout = aiohttp.ClientTimeout(total=10)
    for query in queries:
        url = _DS_SEARCH_URL.format(query)
        try:
            async with session.get(url, timeout=timeout) as r:
                data = await r.json(content_type=None)
        except Exception:
            continue

        for p in data.get("pairs") or []:
            if p.get("chainId") != "ethereum":
                continue
            if not _dex_matches(p.get("dexId", ""), dex_id):
                continue
            if not _pair_matches(p, base, quote):
                continue
            liq = (p.get("liquidity") or {}).get("usd", 0) or 0
            if liq < MIN_POOL_LIQUIDITY:
                continue
            if liq > best_liquidity:
                best_liquidity = liq
                best_address   = p.get("pairAddress")

    async with _pool_cache_lock:
        _pool_cache[cache_key] = best_address

    if best_address:
        print(f"  [discovery] {dex_id}/{pair} → {best_address}  liq=${best_liquidity:,.0f}")
    else:
        print(f"  [discovery] {dex_id}/{pair} → no qualifying pool found")

    return best_address

async def _resolve_pool(
    session: aiohttp.ClientSession,
    dex_id:  str,
    pair:    str,
) -> str | None:
    static = _KNOWN_POOLS.get(dex_id, {}).get(pair)
    if static:
        return static
    return await _discover_pool(session, dex_id, pair)

# ─── Price Fetcher ────────────────────────────────────────────────────────────

async def _fetch_price(
    session: aiohttp.ClientSession,
    dex_id:  str,
    pair:    str,
) -> float | None:
    """Fetch USD price for `pair` from `dex_id` via DexScreener. Returns None on failure."""
    pool = await _resolve_pool(session, dex_id, pair)
    if not pool:
        return None

    url     = _DS_PAIRS_URL.format(pool)
    timeout = aiohttp.ClientTimeout(total=10)
    async with session.get(url, timeout=timeout) as r:
        data = await r.json(content_type=None)

    pairs_data = data.get("pairs") or []
    if not pairs_data:
        return None

    base, quote = pair.split("/")
    for entry in pairs_data:
        if not _pair_matches(entry, base, quote):
            continue

        price_str = entry.get("priceUsd")
        if not price_str:
            continue

        raw = float(price_str)
        if raw <= 0:
            continue

        bt = _normalize_symbol(entry.get("baseToken", {}).get("symbol", ""))
        b  = _normalize_symbol(base)

        if bt == b:
            return raw
        # Base token in the pool is NOT ETH (e.g. LINK/WETH, PEPE/WETH, WBTC/WETH).
        # DexScreener priceUsd = USD price of the non-ETH base (e.g. $9.09 for LINK).
        # DexScreener priceNative = price of base in ETH (e.g. 0.0044 ETH per LINK).
        # ETH/USD = priceUsd / priceNative  →  $9.09 / 0.0044 ≈ $2066
        native_str = entry.get("priceNative")
        if native_str:
            try:
                price_native = float(native_str)
                if price_native > 1e-18 and raw > 0:
                    eth_usd = raw / price_native
                    if eth_usd > 0:
                        return eth_usd
            except (ValueError, ZeroDivisionError):
                pass
        # Legacy fallback for stablecoin-quoted pools where inversion is safe
        return (1.0 / raw) if raw > 1e-12 else None

    # Fallback: first entry with a positive priceUsd
    for entry in pairs_data:
        price_str = entry.get("priceUsd")
        if price_str:
            raw = float(price_str)
            if raw > 0:
                return raw

    return None

# ─── Arbitrage Detection ──────────────────────────────────────────────────────

def _fresh_prices(pair: str) -> dict[str, float]:
    """Return {dex: price} for non-stale, positive quotes."""
    now  = time.time()
    data = prices[pair]
    out: dict[str, float] = {}
    for dex, v in data.items():
        age   = now - v.get("ts", 0)
        price = v.get("price", 0)
        if price <= 0:
            continue
        if age <= MAX_QUOTE_AGE:
            out[dex] = price
        else:
            key = (dex, pair)
            if now - last_stale_log[key] >= STALE_LOG_EVERY:
                print(f"  [stale] {dex}/{pair}  age={age:.1f}s — skipped")
                last_stale_log[key] = now
    return out


def check_cross_version_arb(pair: str) -> None:
    """Detect cross-version (V4 vs V3) arbitrage for a single trading pair."""
    fresh = _fresh_prices(pair)
    if len(fresh) < 2:
        return

    min_dex = min(fresh, key=fresh.get)   # type: ignore[arg-type]
    max_dex = max(fresh, key=fresh.get)   # type: ignore[arg-type]
    if min_dex == max_dex:
        return

    buy_price  = fresh[min_dex]
    sell_price = fresh[max_dex]
    if buy_price <= 0:
        return

    gross_pct = (sell_price - buy_price) / buy_price * 100
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

    ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    now = time.time()

    print(f"\n{'='*64}")
    print(f"  CROSS-VERSION ARB  {pair}  [{ts}]")
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
    for dex, v in sorted(prices[pair].items()):
        age = f"{now - v['ts']:.1f}" if v.get("ts") else "—"
        print(f"  {dex:<16} {v['price']:>14.4f} {age:>8}")
    print()

    log_alert(ts, "cross_version", pair,
              min_dex, buy_price, max_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, MIN_TRADE_USDT, profit_usdt)


# Triangular arb is only meaningful when all legs are in the same currency.
# Stablecoin-quoted pairs (USDC/USDT/DAI) give a direct ETH/USD price from
# DexScreener without relying on priceNative accuracy.  Non-stablecoin pairs
# (WBTC, LINK, PEPE …) are still monitored for cross-version arb but are
# deliberately excluded from triangular comparisons to avoid spurious signals.
_STABLECOIN_QUOTES = {"USDC", "USDT", "DAI"}

def _is_stablecoin_pair(pair: str) -> bool:
    return pair.split("/")[1].upper() in _STABLECOIN_QUOTES


def check_triangular_arb() -> None:
    """
    Triangular arbitrage across stablecoin-quoted ETH pairs.

    Restricted to ETH/USDC, ETH/USDT, ETH/DAI so all implied ETH prices are
    in the same unit (USD) and can be compared directly.  Leg-3 of the trade
    is always a stablecoin swap (e.g. USDC→DAI) which is cheap and easy to
    size, making the profit estimate reliable.

    Non-stablecoin pairs (WBTC, LINK, PEPE …) are excluded because:
      • Their "ETH price" depends on priceNative accuracy from DexScreener
      • Leg-3 would be a volatile-asset swap with unpredictable cost/slippage
    """
    eth_price_per_pair: dict[str, float] = {}
    for pair in PAIRS:
        if not _is_stablecoin_pair(pair):
            continue
        fresh = _fresh_prices(pair)
        if not fresh:
            continue
        vals = sorted(v for v in fresh.values() if v > 0)
        if not vals:
            continue
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        # Sanity range: plausible ETH/USD price ($10 – $1,000,000)
        if not (10 < median < 1_000_000):
            continue
        eth_price_per_pair[pair] = median

    if len(eth_price_per_pair) < 2:
        return

    for pair_a, pair_b in combinations(eth_price_per_pair, 2):
        price_a = eth_price_per_pair[pair_a]
        price_b = eth_price_per_pair[pair_b]

        if price_a <= price_b:
            buy_pair,  sell_pair  = pair_a, pair_b
            buy_price, sell_price = price_a, price_b
        else:
            buy_pair,  sell_pair  = pair_b, pair_a
            buy_price, sell_price = price_b, price_a

        # Both prices must be positive and sensible
        if buy_price <= 0 or sell_price <= 0:
            continue

        gross_pct = (sell_price - buy_price) / buy_price * 100
        if gross_pct < TRI_ARB_THRESHOLD:
            continue

        fee_total = 0.60   # two swaps at ~0.30% each
        net_pct   = gross_pct - fee_total

        alert_key = f"tri:{buy_pair}:{sell_pair}"
        mono_now  = time.monotonic()
        if mono_now - last_alert[alert_key] < ALERT_COOLDOWN:
            continue
        last_alert[alert_key] = mono_now

        ts          = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        qty         = MIN_TRADE_USDT / buy_price
        proceeds    = qty * sell_price * (1 - 0.003)
        cost        = MIN_TRADE_USDT  * 1.003
        profit_usdt = proceeds - cost

        print(f"\n{'*'*64}")
        print(f"  TRIANGULAR ARB SIGNAL  [{ts}]")
        print(f"{'*'*64}")
        print(f"  ETH implied cheaper via  {buy_pair:<12}  @ {buy_price:>10.4f} USD")
        print(f"  ETH implied pricier via  {sell_pair:<12}  @ {sell_price:>10.4f} USD")
        print(f"  Implied gross spread : {gross_pct:>8.4f}%")
        print(f"  Est. fees (2 swaps)  : {fee_total:>8.2f}%")
        print(f"  Net spread           : {net_pct:>8.4f}%  {'✓ CHECK LEG-3 COST' if net_pct > 0 else '✗ fee-negative'}")
        print(f"  Est. gross profit    : ${profit_usdt:>8.4f}  on ${MIN_TRADE_USDT:,.0f} (excl. leg-3 swap)")
        print(f"  NOTE: Verify leg-3 (token swap) cost before executing.")
        print(f"{'*'*64}\n")

        log_alert(ts, "triangular", f"{buy_pair}↔{sell_pair}",
                  buy_pair, buy_price, sell_pair, sell_price,
                  gross_pct, 0.30, 0.30, net_pct, MIN_TRADE_USDT, profit_usdt)

# ─── DEX Polling ──────────────────────────────────────────────────────────────

FETCHERS = list(DEX_FEE_PCT.keys())  # ["uniswap_v4", "uniswap_v3"]

async def poll_dex(dex_id: str, pair: str) -> None:
    retry_delay = 2
    failures    = 0
    last_err    = ""

    print(f"[{dex_id}] Polling {pair}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                price = await _fetch_price(session, dex_id, pair)
                if price and price > 0:
                    prices[pair][dex_id] = {"price": price, "ts": time.time()}
                    check_cross_version_arb(pair)
                    check_triangular_arb()
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
        print(f"  PRICE SNAPSHOT  {datetime.now().strftime('%H:%M:%S')}  (Uniswap V4/V3)")
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
    print("║         DEX Arbitrage Monitor — Uniswap V4 / V3          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Pairs      : {', '.join(PAIRS)}")
    print(f"  DEXes      : Uniswap V4 (primary) + Uniswap V3 (comparison/fallback)")
    print(f"  Arb types  : cross-version (V4 vs V3) + triangular (across ETH pairs)")
    print(f"  Threshold  : {MIN_SPREAD_PCT}% gross (cross-version) / {TRI_ARB_THRESHOLD}% (triangular)")
    print(f"  Min trade  : ${MIN_TRADE_USDT:,.0f} USD")
    print(f"  Min liq    : ${MIN_POOL_LIQUIDITY:,.0f} USD")
    print(f"  Poll every : {POLL_INTERVAL}s  (≈ 1 Ethereum block)")
    print(f"  Alert log  : {ALERT_LOG_FILE}")
    print(f"  V4 PoolMgr : 0x000000000004444c5dc75cB358380D2e3dE08A90")
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
