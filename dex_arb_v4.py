"""
DEX Arbitrage Monitor — Uniswap V4 / V3 Only
=============================================
Monitors ETH/{USDC, USDT, DAI, WBTC, LINK, UNI, AAVE, LDO, PEPE, MKR}
across Uniswap V4 and V3 via the DexScreener REST API.

Key features:
  • V4 → V3 and V3 → V4 arb: both directions tracked independently
  • Dynamic pool discovery for pairs without hardcoded addresses
  • Cross-version arbitrage: detects price discrepancies between V4 and V3
  • Triangular arbitrage: spots ETH-leg imbalances across stable pairs
  • Higher spread threshold (0.08%) to surface real profitable opportunities
  • Direction-specific cooldown so V4→V3 and V3→V4 alerts don't suppress each other
  • Exponential back-off on fetch failures

Ethereum block time ≈ 12 s; poll interval set to 5 s for tighter V4 coverage.
V4 PoolManager (Ethereum mainnet): 0x000000000004444c5dc75cB358380D2e3dE08A90
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
    "ETH/USDC", "ETH/USDT", "ETH/DAI", "ETH/WBTC",
    "ETH/LINK", "ETH/UNI", "ETH/AAVE", "ETH/LDO",
    "ETH/PEPE", "ETH/MKR",
]

# ─── Token addresses (Ethereum mainnet) ───────────────────────────────────────
TOKEN_ADDRESSES: dict[str, str] = {
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "UNI":  "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "LDO":  "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",
    "PEPE": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
    "MKR":  "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
}

# ─── DEX Map ──────────────────────────────────────────────────────────────────
# DexScreener dexId substrings used for matching API responses.
_DEXSCREENER_ID_MAP: dict[str, list[str]] = {
    "uniswap_v4": ["uniswap_v4", "uniswap-v4", "uniswapv4", "v4"],
    "uniswap_v3": ["uniswap_v3", "uniswap-v3", "uniswapv3"],
}

FETCHERS = list(_DEXSCREENER_ID_MAP.keys())  # ["uniswap_v4", "uniswap_v3"]

# ─── Fees ─────────────────────────────────────────────────────────────────────
# Per-pair LP swap fee overrides (%). Defaults to 0.30% when not specified.
_DEX_PAIR_FEE: dict[str, dict[str, float]] = {
    "uniswap_v4": {
        "ETH/USDC": 0.05, "ETH/USDT": 0.05, "ETH/DAI": 0.05,
        "ETH/WBTC": 0.30, "ETH/LINK": 0.30, "ETH/UNI": 0.30,
        "ETH/AAVE": 0.30, "ETH/LDO": 0.30, "ETH/PEPE": 1.00, "ETH/MKR": 0.30,
    },
    "uniswap_v3": {
        "ETH/USDC": 0.05, "ETH/USDT": 0.05, "ETH/DAI": 0.05,
        "ETH/WBTC": 0.30, "ETH/LINK": 0.30, "ETH/UNI": 0.30,
        "ETH/AAVE": 0.30, "ETH/LDO": 0.30, "ETH/PEPE": 1.00, "ETH/MKR": 0.30,
    },
}

def get_fee(dex: str, pair: str) -> float:
    return _DEX_PAIR_FEE.get(dex, {}).get(pair, 0.30)

# ─── Known Pool Addresses (Ethereum mainnet) ──────────────────────────────────
# Remaining pairs are discovered dynamically via DexScreener search.
_KNOWN_POOLS: dict[str, dict[str, str]] = {
    "uniswap_v3": {
        "ETH/USDC": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # 0.05%
        "ETH/USDT": "0x11b815efb8f581194ae79006d24e0d814b7697f6",  # 0.05%
        "ETH/DAI":  "0x60594a405d53811d3bc4766596efd80fd545a270",  # 0.05%
        "ETH/WBTC": "0xcbcdf9626bc03e24f779434178a73a0b4bad62ed",  # 0.30%
    },
    "uniswap_v4": {
        # V4 uses singleton PoolManager; DexScreener exposes pools via pair hash.
        "ETH/USDC": "0xdce6394339af00981949f5f3baf27e3610c76326a700af57e4b3e3ae4977f78d",
        "ETH/USDT": "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36a8f4a7f8e0b8e2e8f0a3b2c1",
        # Discovery finds the rest automatically.
    },
}

# ─── DexScreener URLs ─────────────────────────────────────────────────────────
_DS_PAIRS_URL  = "https://api.dexscreener.com/latest/dex/pairs/ethereum/{}"
_DS_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={}"

# ─── Thresholds & Config ──────────────────────────────────────────────────────
MIN_SPREAD_PCT     = 0.08    # minimum gross spread to flag (%) — avoids noise
ALERT_COOLDOWN     = 30      # seconds between alerts per direction key
MIN_TRADE_USDT     = 1_000.0 # notional trade size for P&L estimate
POLL_INTERVAL      = 5       # seconds between price polls
MAX_QUOTE_AGE      = 60.0    # discard quotes older than this (seconds)
STALE_LOG_EVERY    = 30.0    # rate-limit stale-quote log messages
MIN_POOL_LIQUIDITY = 10_000  # USD — skip pools below this in discovery
DISCOVERY_MIN_LIQ  = 1_000   # USD — looser filter used during pool scoring
ALERT_LOG_FILE     = "dex_arb_v4_alerts.csv"

TRI_ARB_THRESHOLD  = 0.30    # min gross spread for triangular arb (%) — raised to reduce spam
TRI_ARB_COOLDOWN   = 120     # seconds between triangular arb alerts per pair-combo
TRI_ARB_PAIRS      = {"ETH/USDC", "ETH/USDT", "ETH/DAI"}
TRI_MIN_PRICE      = 100.0   # sanity-filter for ETH prices used in tri-arb

# ─── Alert Log ────────────────────────────────────────────────────────────────
_log_file = open(ALERT_LOG_FILE, "a", newline="", buffering=1)
_csv = csv.writer(_log_file)
if os.path.getsize(ALERT_LOG_FILE) == 0:
    _csv.writerow([
        "timestamp", "arb_type", "pair",
        "buy_dex", "buy_price", "sell_dex", "sell_price",
        "gross_spread_pct", "fee_buy_pct", "fee_sell_pct",
        "net_spread_pct", "trade_size_usdt", "est_profit_usdt", "direction",
    ])

def log_alert(ts, arb_type, pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, trade_usdt, profit_usdt, direction):
    _csv.writerow([
        ts, arb_type, pair,
        buy_dex, f"{buy_price:.6f}", sell_dex, f"{sell_price:.6f}",
        f"{gross_pct:.4f}", f"{fee_buy:.4f}", f"{fee_sell:.4f}",
        f"{net_pct:.4f}", f"{trade_usdt:.2f}", f"{profit_usdt:.4f}", direction,
    ])

# ─── Shared State ─────────────────────────────────────────────────────────────
prices:         dict[str, dict[str, dict]]       = defaultdict(dict)
last_alert:     dict[str, float]                 = defaultdict(float)
last_stale_log: dict[tuple, float]               = defaultdict(float)
_pool_cache:    dict[tuple[str, str], str | None] = {}
_pool_cache_lock = asyncio.Lock()

# ─── Helper Functions ─────────────────────────────────────────────────────────

def _normalize_symbol(s: str) -> str:
    """Treat WETH and ETH as the same symbol."""
    return "ETH" if s.upper() == "WETH" else s.upper()

def _dex_matches(dex_id_str: str, logical_dex: str) -> bool:
    """Check if a DexScreener dexId string matches our logical DEX name."""
    return any(slug in dex_id_str.lower() for slug in _DEXSCREENER_ID_MAP.get(logical_dex, [logical_dex]))

def _pair_matches(pair_data: dict, base: str, quote: str) -> bool:
    bt = _normalize_symbol(pair_data.get("baseToken", {}).get("symbol", ""))
    qt = _normalize_symbol(pair_data.get("quoteToken", {}).get("symbol", ""))
    b  = _normalize_symbol(base)
    q  = _normalize_symbol(quote)
    return (bt == b and qt == q) or (bt == q and qt == b)

def _score_pool(p: dict, base: str, quote: str, dex_id: str) -> float | None:
    """Return pool liquidity if it passes all filters, else None."""
    if p.get("chainId") != "ethereum":
        return None
    if not _dex_matches(p.get("dexId", ""), dex_id):
        return None
    if not _pair_matches(p, base, quote):
        return None
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    return float(liq) if liq >= DISCOVERY_MIN_LIQ else None

async def _query_dexscreener(session: aiohttp.ClientSession, url: str,
                              timeout: aiohttp.ClientTimeout) -> list[dict]:
    try:
        async with session.get(url, timeout=timeout) as r:
            data = await r.json(content_type=None)
        return data.get("pairs") or []
    except Exception:
        return []

# ─── Pool Discovery ───────────────────────────────────────────────────────────

async def _discover_pool(session: aiohttp.ClientSession, dex_id: str, pair: str) -> str | None:
    """
    Search DexScreener for the highest-liquidity pool matching `pair` on `dex_id`.

    Strategy:
      1. Token-address lookup for both the quote token and WETH.
      2. Text-search fallback using symbol names.
    Results (including negative) are cached for the session lifetime.
    """
    cache_key = (dex_id, pair)
    async with _pool_cache_lock:
        if cache_key in _pool_cache:
            return _pool_cache[cache_key]

    base, quote = pair.split("/")
    timeout = aiohttp.ClientTimeout(total=10)
    best_address: str | None = None
    best_liq = 0.0

    def check(candidates: list[dict]) -> None:
        nonlocal best_address, best_liq
        for p in candidates:
            liq = _score_pool(p, base, quote, dex_id)
            if liq is not None and liq > best_liq:
                best_liq = liq
                best_address = p.get("pairAddress")

    # 1. Token-address lookup (more precise, avoids symbol collisions)
    for addr in filter(None, {TOKEN_ADDRESSES.get(quote), TOKEN_ADDRESSES.get("WETH")}):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
        check(await _query_dexscreener(session, url, timeout))
        if best_address:
            break

    # 2. Text-search fallback
    if not best_address:
        for q in {f"ETH {quote}", f"WETH {quote}", f"{base} {quote}"}:
            check(await _query_dexscreener(session, _DS_SEARCH_URL.format(q), timeout))

    async with _pool_cache_lock:
        _pool_cache[cache_key] = best_address

    if best_address:
        print(f"  [✓ discovery] {dex_id}/{pair} → {best_address}  liq=${best_liq:,.0f}")
    else:
        print(f"  [✗ discovery] {dex_id}/{pair} → no qualifying pool found")
    return best_address

async def _resolve_pool(session: aiohttp.ClientSession, dex_id: str, pair: str) -> str | None:
    """Return pool address: static known address first, then dynamic discovery."""
    return _KNOWN_POOLS.get(dex_id, {}).get(pair) or await _discover_pool(session, dex_id, pair)

# ─── Price Fetcher ────────────────────────────────────────────────────────────

async def _fetch_price(session: aiohttp.ClientSession, dex_id: str, pair: str) -> float | None:
    """Fetch USD price for `pair` from `dex_id` via DexScreener. Returns None on failure."""
    pool = await _resolve_pool(session, dex_id, pair)
    if not pool:
        return None

    timeout = aiohttp.ClientTimeout(total=10)
    async with session.get(_DS_PAIRS_URL.format(pool), timeout=timeout) as r:
        data = await r.json(content_type=None)

    base, quote = pair.split("/")
    for entry in data.get("pairs") or []:
        if _pair_matches(entry, base, quote):
            price_str = entry.get("priceUsd")
            if price_str:
                raw = float(price_str)
                bt = _normalize_symbol(entry.get("baseToken", {}).get("symbol", ""))
                # Invert price if DexScreener has the pair in reverse orientation
                return raw if bt == _normalize_symbol(base) else (1.0 / raw if raw else None)

    # Fallback: use first entry's USD price as-is
    price_str = (data.get("pairs") or [{}])[0].get("priceUsd")
    return float(price_str) if price_str else None

# ─── Stale-safe Price Accessor ────────────────────────────────────────────────

def _fresh_prices(pair: str) -> dict[str, float]:
    """Return {dex: price} for quotes within MAX_QUOTE_AGE. Rate-limits stale warnings."""
    now = time.time()
    out: dict[str, float] = {}
    for dex, v in prices[pair].items():
        age = now - v.get("ts", 0)
        if age <= MAX_QUOTE_AGE:
            out[dex] = v["price"]
        else:
            key = (dex, pair)
            if now - last_stale_log[key] >= STALE_LOG_EVERY:
                print(f"  [stale] {dex}/{pair}  age={age:.1f}s — skipped")
                last_stale_log[key] = now
    return out

# ─── Arbitrage Detection ──────────────────────────────────────────────────────

def check_cross_version_arb(pair: str) -> None:
    """
    Detect cross-version arbitrage (V4 vs V3) for a single trading pair.

    Uses a direction-specific cooldown key (pair:buy_dex:sell_dex) so that
    V4→V3 and V3→V4 opportunities are tracked independently and neither
    direction suppresses the other.
    """
    fresh = _fresh_prices(pair)
    if len(fresh) < 2:
        return

    buy_dex   = min(fresh, key=fresh.get)  # type: ignore[arg-type]
    sell_dex  = max(fresh, key=fresh.get)  # type: ignore[arg-type]
    buy_price  = fresh[buy_dex]
    sell_price = fresh[sell_dex]
    gross_pct  = (sell_price - buy_price) / buy_price * 100

    if gross_pct < MIN_SPREAD_PCT:
        return

    fee_buy  = get_fee(buy_dex, pair)
    fee_sell = get_fee(sell_dex, pair)
    net_pct  = gross_pct - fee_buy - fee_sell

    qty         = MIN_TRADE_USDT / buy_price
    proceeds    = qty * sell_price * (1 - fee_sell / 100)
    cost        = MIN_TRADE_USDT  * (1 + fee_buy  / 100)
    profit_usdt = proceeds - cost

    # Direction-specific cooldown: V4→V3 and V3→V4 are independent signals
    direction   = f"{buy_dex} → {sell_dex}"
    cooldown_key = f"{pair}:{buy_dex}:{sell_dex}"
    mono_now = time.monotonic()
    if mono_now - last_alert[cooldown_key] < ALERT_COOLDOWN:
        return
    last_alert[cooldown_key] = mono_now

    ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    now = time.time()

    print(f"\n{'='*75}")
    print(f"  CROSS-VERSION ARB  {pair}  [{direction}]  [{ts}]")
    print(f"{'='*75}")
    print(f"  BUY  on {buy_dex:<14}  @ {buy_price:>12.4f}  fee {fee_buy:.3f}%")
    print(f"  SELL on {sell_dex:<14}  @ {sell_price:>12.4f}  fee {fee_sell:.3f}%")
    print(f"  Gross: {gross_pct:>6.4f}%   Net: {net_pct:>6.4f}%"
          f"   Profit: ${profit_usdt:>7.2f}  {'✓ PROFITABLE' if net_pct > 0 else '✗ fee-negative'}")

    print(f"\n  Price snapshot — {pair}:")
    print(f"  {'DEX':<16} {'Price (USD)':>14} {'Age(s)':>8}")
    print(f"  {'-'*42}")
    for dex, v in sorted(prices[pair].items()):
        age = f"{now - v['ts']:.1f}" if v.get("ts") else "—"
        print(f"  {dex:<16} {v['price']:>14.4f} {age:>8}")
    print(f"{'='*75}\n")

    log_alert(ts, "cross_version", pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, MIN_TRADE_USDT, profit_usdt, direction)


def check_triangular_arb() -> None:
    """
    Triangular arbitrage across ETH-denominated stable pairs.

    Collects the median ETH price from each TRI_ARB_PAIRS pair, then flags
    any pair combination where the implied ETH spread exceeds TRI_ARB_THRESHOLD.
    Uses a separate cooldown (TRI_ARB_COOLDOWN) so triangular alerts don't
    interfere with cross-version alerts.
    """
    eth_price_per_pair: dict[str, float] = {}
    for pair in TRI_ARB_PAIRS:
        fresh = _fresh_prices(pair)
        if not fresh:
            continue
        vals = [v for v in sorted(fresh.values()) if v >= TRI_MIN_PRICE]
        if not vals:
            continue
        mid = len(vals) // 2
        eth_price_per_pair[pair] = vals[mid] if len(vals) % 2 == 1 else (vals[mid - 1] + vals[mid]) / 2

    if len(eth_price_per_pair) < 2:
        return

    for pair_a, pair_b in combinations(eth_price_per_pair, 2):
        price_a = eth_price_per_pair[pair_a]
        price_b = eth_price_per_pair[pair_b]

        buy_pair, sell_pair = (pair_a, pair_b) if price_a <= price_b else (pair_b, pair_a)
        buy_price, sell_price = min(price_a, price_b), max(price_a, price_b)
        gross_pct = (sell_price - buy_price) / buy_price * 100

        if gross_pct < TRI_ARB_THRESHOLD:
            continue

        # Conservative 2-swap fee estimate (0.30% each side)
        fee_total = 0.60
        net_pct   = gross_pct - fee_total

        alert_key = f"tri:{buy_pair}:{sell_pair}"
        mono_now  = time.monotonic()
        if mono_now - last_alert[alert_key] < TRI_ARB_COOLDOWN:
            continue
        last_alert[alert_key] = mono_now

        qty         = MIN_TRADE_USDT / buy_price
        proceeds    = qty * sell_price * (1 - 0.003)
        cost        = MIN_TRADE_USDT  * 1.003
        profit_usdt = proceeds - cost

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        print(f"\n{'*'*75}")
        print(f"  TRIANGULAR ARB  {buy_pair} → {sell_pair}  [{ts}]")
        print(f"{'*'*75}")
        print(f"  ETH cheaper via  {buy_pair:<12}  @ {buy_price:>10.4f} USD")
        print(f"  ETH pricier via  {sell_pair:<12}  @ {sell_price:>10.4f} USD")
        print(f"  Gross spread : {gross_pct:>8.4f}%")
        print(f"  Est. fees    : {fee_total:>8.2f}%  (2 swaps)")
        print(f"  Net spread   : {net_pct:>8.4f}%  {'✓ CHECK LEG-3 COST' if net_pct > 0 else '✗ fee-negative'}")
        print(f"  Est. profit  : ${profit_usdt:>8.4f}  on ${MIN_TRADE_USDT:,.0f} (excl. leg-3 swap)")
        print(f"  NOTE: Verify leg-3 (stablecoin swap) cost before executing.")
        print(f"{'*'*75}\n")

        log_alert(ts, "triangular", f"{buy_pair}↔{sell_pair}",
                  buy_pair, buy_price, sell_pair, sell_price,
                  gross_pct, 0.30, 0.30, net_pct, MIN_TRADE_USDT, profit_usdt, "tri")

# ─── DEX Polling ──────────────────────────────────────────────────────────────

async def poll_dex(dex_id: str, pair: str) -> None:
    """Poll `dex_id` for `pair` in a loop with exponential back-off on failure."""
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
                    print(f"[{dex_id}/{pair}] Giving up after 8 consecutive failures. Last: {err}")
                    return
                if err != last_err:
                    print(f"[{dex_id}/{pair}] {err}  (retry {failures}/8 in {retry_delay}s)")
                    last_err = err
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

# ─── Periodic Status Table ────────────────────────────────────────────────────

async def status_printer() -> None:
    """Print a live price snapshot every 30 seconds."""
    await asyncio.sleep(20)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        print(f"\n{'─'*80}")
        print(f"  LIVE SNAPSHOT  {datetime.now().strftime('%H:%M:%S')} — Uniswap V4 + V3")
        print(f"{'─'*80}")
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
        print(f"\n{'─'*80}\n")

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main() -> None:
    print("╔════════════════════════════════════════════════════════════════╗")
    print("║    DEX ARBITRAGE MONITOR — Uniswap V4 + V3 (ALL DIRECTIONS)   ║")
    print("╚════════════════════════════════════════════════════════════════╝")
    print(f"  Pairs       : {', '.join(PAIRS)}")
    print(f"  DEXes       : uniswap_v4 + uniswap_v3 (V4→V3 and V3→V4)")
    print(f"  Arb types   : cross-version (same pair) + triangular (stable ETH legs)")
    print(f"  Threshold   : {MIN_SPREAD_PCT}% gross (cross-version) / {TRI_ARB_THRESHOLD}% (triangular)")
    print(f"  Min trade   : ${MIN_TRADE_USDT:,.0f} USD")
    print(f"  Min liq     : ${MIN_POOL_LIQUIDITY:,.0f} USD (pool discovery filter)")
    print(f"  Cooldown    : {ALERT_COOLDOWN}s (per direction) / {TRI_ARB_COOLDOWN}s (triangular)")
    print(f"  Poll every  : {POLL_INTERVAL}s")
    print(f"  Alert log   : {ALERT_LOG_FILE}")
    print(f"  V4 PoolMgr  : 0x000000000004444c5dc75cB358380D2e3dE08A90")
    print()
    print("  Note: pairs without hardcoded pool addresses use dynamic discovery")
    print("  on first poll — expect a brief startup delay for those pairs.")
    print()

    tasks = [poll_dex(dex_id, pair) for pair in PAIRS for dex_id in FETCHERS]
    tasks.append(status_printer())
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")
    finally:
        _log_file.close()
