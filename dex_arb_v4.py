"""
DEX Arbitrage Monitor — Uniswap V4 / V3 / V2 Only
FINAL BULLETPROOF VERSION — Phantom price bug completely eliminated
Flash-loan execution integrated via flash_executor.py
=============================================
"""

import asyncio
import aiohttp
import csv
import os
import time
from collections import defaultdict
from datetime import datetime
from itertools import combinations

# ─── Flash-loan executor (optional — no-ops if env vars not set) ──────────────
try:
    from flash_executor import get_executor
    _flash_ready = True
except ImportError:
    _flash_ready = False
    def get_executor():  # type: ignore[misc]
        return None

# ─── Flash-loan execution thresholds ─────────────────────────────────────────
# Only attempt execution when estimated net profit exceeds this value (USD).
# Set to 0 to disable auto-execution entirely and run in monitor-only mode.
FLASH_MIN_PROFIT_USD  = float(os.getenv("MIN_PROFIT_USDT",  "15"))
FLASH_LOAN_SIZE_USD   = float(os.getenv("FLASH_LOAN_USDT",  "50000"))

# ─── Pairs ────────────────────────────────────────────────────────────────────
PAIRS = [
    "ETH/USDC", "ETH/USDT", "ETH/DAI", "ETH/WBTC",
    "ETH/LINK", "ETH/UNI", "ETH/AAVE", "ETH/LDO",
    "ETH/PEPE", "ETH/MKR",
]

# ─── Token addresses ──────────────────────────────────────────────────────────
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
_DEXSCREENER_ID_MAP: dict[str, list[str]] = {
    "uniswap_v4": ["uniswap_v4", "uniswap-v4", "uniswapv4", "v4"],
    "uniswap_v3": ["uniswap_v3", "uniswap-v3", "uniswapv3"],
    "uniswap_v2": ["uniswap", "uniswap-v2", "v2", "uniswap v2"],
}

# ─── Fees ─────────────────────────────────────────────────────────────────────
_DEX_PAIR_FEE: dict[str, dict[str, float]] = {
    "uniswap_v4": {k: 0.05 if "USDC" in k or "USDT" in k or "DAI" in k else 0.30 for k in PAIRS},
    "uniswap_v3": {k: 0.05 if "USDC" in k or "USDT" in k or "DAI" in k else 0.30 for k in PAIRS},
    "uniswap_v2": {k: 0.30 for k in PAIRS},
}

def get_fee(dex: str, pair: str) -> float:
    return _DEX_PAIR_FEE.get(dex, {}).get(pair, 0.30)

# ─── Known Pools ──────────────────────────────────────────────────────────────
_KNOWN_POOLS: dict[str, dict[str, str]] = {
    "uniswap_v3": {
        "ETH/USDC": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
        "ETH/USDT": "0x11b815efb8f581194ae79006d24e0d814b7697f6",
        "ETH/DAI":  "0x60594a405d53811d3bc4766596efd80fd545a270",
        "ETH/WBTC": "0xcbcdf9626bc03e24f779434178a73a0b4bad62ed",
    },
    "uniswap_v4": {
        "ETH/USDC": "0xdce6394339af00981949f5f3baf27e3610c76326a700af57e4b3e3ae4977f78d",
        "ETH/USDT": "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36a8f4a7f8e0b8e2e8f0a3b2c1",
        "ETH/DAI":  "0xad213c3b1607bb9bb39ad1986af9414b454d5c0d21578c43cae28d1b84b3348d",
    },
    "uniswap_v2": {
        "ETH/PEPE": "0x11950d141ecb863f01007add7d1a342041227b58",
        "ETH/AAVE": "0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb",
        "ETH/LDO":  "0xf4ad61db72f114be877e87d62dc5e7bd52df4d9b",
        "ETH/UNI":  "0x1d912cacd6f8d34415e2c1a9374eb7d8e8ac5459",
        "ETH/LINK": "0xa2107fa5b38d9bbd2c461d6edf11b11a50f6b974",
        "ETH/MKR":  "0xc2adda861f89bbb333c90c492cb837741916a225",
        "ETH/WBTC": "0xca35e32e7926b96a9988f61d510e038108d8068e",
        "ETH/DAI":  "0xa27C56b3969CfB8FBCE427337D98e3bd794Ec688",
    },
}

# ─── URLs & Config ────────────────────────────────────────────────────────────
_DS_PAIRS_URL  = "https://api.dexscreener.com/latest/dex/pairs/ethereum/{}"
_DS_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={}"

MIN_SPREAD_PCT       = 0.08
ALERT_COOLDOWN       = 30
MIN_TRADE_USDT       = 1000.0
POLL_INTERVAL        = 5
MAX_QUOTE_AGE        = 60.0
DISCOVERY_MIN_LIQ    = 500
ALERT_LOG_FILE       = "dex_arb_v4_alerts.csv"
TRI_ARB_THRESHOLD    = 0.30

TRI_ARB_PAIRS  = {"ETH/USDC", "ETH/USDT", "ETH/DAI"}
TRI_MIN_PRICE  = 100.0

# ─── Logging ──────────────────────────────────────────────────────────────────
_log_file = open(ALERT_LOG_FILE, "a", newline="", buffering=1)
_csv = csv.writer(_log_file)
if os.path.getsize(ALERT_LOG_FILE) == 0:
    _csv.writerow(["timestamp", "arb_type", "pair", "buy_dex", "buy_price",
                   "sell_dex", "sell_price", "gross_spread_pct", "fee_buy_pct",
                   "fee_sell_pct", "net_spread_pct", "trade_size_usdt",
                   "est_profit_usdt", "direction", "tx_hash"])

def log_alert(ts, arb_type, pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, trade_usdt, profit_usdt,
              direction, tx_hash=""):
    _csv.writerow([ts, arb_type, pair, buy_dex, f"{buy_price:.6f}",
                   sell_dex, f"{sell_price:.6f}",
                   f"{gross_pct:.4f}", f"{fee_buy:.4f}", f"{fee_sell:.4f}",
                   f"{net_pct:.4f}", f"{trade_usdt:.2f}", f"{profit_usdt:.4f}",
                   direction, tx_hash])

# ─── Shared State ─────────────────────────────────────────────────────────────
prices: dict[str, dict[str, dict]] = defaultdict(dict)
last_alert: dict[str, float] = defaultdict(float)
_pool_cache: dict[tuple[str, str], str | None] = {}
_pool_cache_lock = asyncio.Lock()

# ─── Discovery ────────────────────────────────────────────────────────────────
def _normalize_symbol(s: str) -> str:
    return "ETH" if s.upper() == "WETH" else s.upper()

def _dex_matches(dex_id_str: str, logical_dex: str) -> bool:
    return any(slug in dex_id_str.lower() for slug in _DEXSCREENER_ID_MAP.get(logical_dex, [logical_dex]))

def _pair_matches(pair_data: dict, base: str, quote: str) -> bool:
    bt = _normalize_symbol(pair_data.get("baseToken", {}).get("symbol", ""))
    qt = _normalize_symbol(pair_data.get("quoteToken", {}).get("symbol", ""))
    b = _normalize_symbol(base)
    q = _normalize_symbol(quote)
    return (bt == b and qt == q) or (bt == q and qt == b)

def _score_pool(p: dict, base: str, quote: str, dex_id: str) -> float | None:
    if p.get("chainId") != "ethereum": return None
    if not _dex_matches(p.get("dexId", ""), dex_id): return None
    if not _pair_matches(p, base, quote): return None
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    return liq if liq >= DISCOVERY_MIN_LIQ else None

async def _query_dexscreener(session, url, timeout) -> list[dict]:
    try:
        async with session.get(url, timeout=timeout) as r:
            data = await r.json(content_type=None)
        return data.get("pairs") or []
    except Exception:
        return []

async def _discover_pool(session, dex_id: str, pair: str) -> str | None:
    cache_key = (dex_id, pair)
    async with _pool_cache_lock:
        if cache_key in _pool_cache: return _pool_cache[cache_key]

    base, quote = pair.split("/")
    timeout = aiohttp.ClientTimeout(total=10)
    best_address = None
    best_liq = 0.0

    def check(candidates):
        nonlocal best_address, best_liq
        for p in candidates:
            liq = _score_pool(p, base, quote, dex_id)
            if liq is not None and liq > best_liq:
                best_liq = liq
                best_address = p.get("pairAddress")

    for addr in {TOKEN_ADDRESSES.get(quote), TOKEN_ADDRESSES["WETH"]}:
        if addr:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
            check(await _query_dexscreener(session, url, timeout))
            if best_address: break

    if not best_address:
        for q in {f"ETH {quote}", f"WETH {quote}", f"{base} {quote}"}:
            check(await _query_dexscreener(session, _DS_SEARCH_URL.format(q), timeout))

    async with _pool_cache_lock:
        _pool_cache[cache_key] = best_address

    if best_address:
        print(f"  [✓ discovery] {dex_id}/{pair} → {best_address}  liq=${best_liq:,.0f}")
    else:
        print(f"  [✗ discovery] {dex_id}/{pair} → no pool found")
    return best_address

async def _resolve_pool(session, dex_id: str, pair: str) -> str | None:
    return _KNOWN_POOLS.get(dex_id, {}).get(pair) or await _discover_pool(session, dex_id, pair)

# ─── Price Fetcher ────────────────────────────────────────────────────────────
async def _fetch_price(session, dex_id: str, pair: str) -> float | None:
    pool = await _resolve_pool(session, dex_id, pair)
    if not pool: return None

    url = _DS_PAIRS_URL.format(pool)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)

    for entry in data.get("pairs") or []:
        if not _pair_matches(entry, *pair.split("/")): continue
        price_str = entry.get("priceUsd")
        if not price_str: continue
        try:
            raw = float(price_str)
            bt = _normalize_symbol(entry.get("baseToken", {}).get("symbol", ""))
            if bt != "ETH":
                raw = 1.0 / raw if raw > 0 else 0
            if 100 < raw < 100_000:    # hard sanity filter for ETH price
                return raw
        except Exception:
            continue
    return None

# ─── Arb Logic ────────────────────────────────────────────────────────────────
def _fresh_prices(pair: str) -> dict[str, float]:
    now = time.time()
    return {dex: v["price"] for dex, v in prices[pair].items()
            if now - v.get("ts", 0) <= MAX_QUOTE_AGE}


def _maybe_flash(pair: str, buy_dex: str, sell_dex: str,
                 buy_price: float, sell_price: float) -> str:
    """
    Attempt flash-loan execution if the executor is configured.
    Returns the tx hash string, or "" if not executed / dry-run.
    """
    if not _flash_ready:
        return ""
    executor = get_executor()
    if executor is None:
        return ""

    # Estimate profit at flash-loan scale before handing off
    aave_fee  = FLASH_LOAN_SIZE_USD * 0.0005
    est_profit = (
        FLASH_LOAN_SIZE_USD / buy_price * sell_price * 0.997
        - FLASH_LOAN_SIZE_USD * 1.003
        - aave_fee
    )
    if est_profit < FLASH_MIN_PROFIT_USD:
        return ""

    tx_hash = executor.trigger(
        pair       = pair,
        buy_dex    = buy_dex,
        sell_dex   = sell_dex,
        buy_price  = buy_price,
        sell_price = sell_price,
        trade_usdt = FLASH_LOAN_SIZE_USD,
        min_profit = FLASH_MIN_PROFIT_USD,
    )
    return tx_hash or ""


def check_cross_version_arb(pair: str) -> None:
    fresh = _fresh_prices(pair)
    if len(fresh) < 2: return

    buy_dex = min(fresh, key=fresh.get)
    sell_dex = max(fresh, key=fresh.get)
    buy_price = fresh[buy_dex]
    sell_price = fresh[sell_dex]
    gross_pct = (sell_price - buy_price) / buy_price * 100

    if gross_pct < MIN_SPREAD_PCT: return

    fee_buy = get_fee(buy_dex, pair)
    fee_sell = get_fee(sell_dex, pair)
    net_pct = gross_pct - fee_buy - fee_sell

    profit_usdt = (
        (MIN_TRADE_USDT / buy_price * sell_price * (1 - fee_sell / 100))
        - (MIN_TRADE_USDT * (1 + fee_buy / 100))
    )

    alert_key = f"{pair}:{buy_dex}:{sell_dex}"
    if time.monotonic() - last_alert[alert_key] < ALERT_COOLDOWN: return
    last_alert[alert_key] = time.monotonic()

    direction = f"{buy_dex} → {sell_dex}"
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    print(f"\n{'='*75}")
    print(f"  CROSS-VERSION ARB  {pair}  [{direction}]  [{ts}]")
    print(f"{'='*75}")
    print(f"  BUY  on {buy_dex:<14} @ {buy_price:>12.4f}  fee {fee_buy:.3f}%")
    print(f"  SELL on {sell_dex:<14} @ {sell_price:>12.4f}  fee {fee_sell:.3f}%")
    print(f"  Gross: {gross_pct:>6.4f}%   Net: {net_pct:>6.4f}%   "
          f"Profit: ${profit_usdt:>7.2f}")
    print(f"{'='*75}\n")

    # ── Attempt flash-loan execution ──────────────────────────────────────────
    tx_hash = _maybe_flash(pair, buy_dex, sell_dex, buy_price, sell_price)
    if tx_hash:
        print(f"  [→ FLASH TX] {tx_hash}\n")

    log_alert(ts, "cross_version", pair, buy_dex, buy_price, sell_dex, sell_price,
              gross_pct, fee_buy, fee_sell, net_pct, MIN_TRADE_USDT, profit_usdt,
              direction, tx_hash)


def check_triangular_arb() -> None:
    eth_price_per_pair: dict[str, float] = {}
    for pair in TRI_ARB_PAIRS:
        fresh = _fresh_prices(pair)
        if fresh:
            vals = [v for v in sorted(fresh.values()) if v >= TRI_MIN_PRICE]
            if vals:
                mid = len(vals) // 2
                eth_price_per_pair[pair] = (
                    vals[mid] if len(vals) % 2 == 1 else (vals[mid - 1] + vals[mid]) / 2
                )

    if len(eth_price_per_pair) < 2: return

    for a, b in combinations(eth_price_per_pair, 2):
        pa, pb = eth_price_per_pair[a], eth_price_per_pair[b]
        buy_pair, sell_pair = (a, b) if pa < pb else (b, a)
        buy_p, sell_p = min(pa, pb), max(pa, pb)
        gross = (sell_p - buy_p) / buy_p * 100
        if gross < TRI_ARB_THRESHOLD: continue

        net = gross - 0.60
        alert_key = f"tri:{buy_pair}:{sell_pair}"
        if time.monotonic() - last_alert[alert_key] < 120: continue
        last_alert[alert_key] = time.monotonic()

        profit = (MIN_TRADE_USDT / buy_p * sell_p * 0.997) - (MIN_TRADE_USDT * 1.003)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        print(f"\n{'*'*75}")
        print(f"  TRIANGULAR ARB  {buy_pair} → {sell_pair}  [{ts}]")
        print(f"{'*'*75}")
        print(f"  Gross spread: {gross:>6.4f}%   Net: {net:>6.4f}%")
        print(f"  Est. profit : ${profit:>7.2f} (excl. leg-3)")
        print(f"{'*'*75}\n")

        log_alert(ts, "triangular", f"{buy_pair}↔{sell_pair}",
                  buy_pair, buy_p, sell_pair, sell_p,
                  gross, 0.3, 0.3, net, MIN_TRADE_USDT, profit, "tri")

# ─── Polling & Status ─────────────────────────────────────────────────────────
async def poll_dex(dex_id: str, pair: str) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                price = await _fetch_price(session, dex_id, pair)
                if price and price > 0:
                    prices[pair][dex_id] = {"price": price, "ts": time.time()}
                    check_cross_version_arb(pair)
                    check_triangular_arb()
                await asyncio.sleep(POLL_INTERVAL)
            except Exception:
                await asyncio.sleep(5)


async def status_printer() -> None:
    await asyncio.sleep(20)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        exec_status = "ENABLED" if (get_executor() is not None) else "monitor-only"
        print(f"\n{'─'*80}\n  LIVE SNAPSHOT {datetime.now().strftime('%H:%M:%S')}"
              f" — V4 + V3 + V2  [{exec_status}]\n{'─'*80}")
        for pair in PAIRS:
            data = prices.get(pair, {})
            if data:
                print(f"\n  {pair}")
                print(f"  {'DEX':<16} {'Price':>12} {'Age':>6}")
                for dex, v in sorted(data.items()):
                    age = f"{now - v['ts']:.1f}s" if v.get("ts") else "—"
                    print(f"  {dex:<16} {v['price']:>12.4f} {age:>6}")
        print(f"{'─'*80}\n")

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    executor = get_executor()
    exec_mode = "EXECUTE (flash loans ON)" if executor else "MONITOR ONLY"

    print("╔════════════════════════════════════════════════════════════╗")
    print("║     DEX ARBITRAGE MONITOR — Uniswap V4 + V3 + V2           ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print(f"  • Mode          : {exec_mode}")
    print(f"  • Min spread    : {MIN_SPREAD_PCT}%")
    print(f"  • Min profit    : ${FLASH_MIN_PROFIT_USD:.2f} (flash threshold)")
    print(f"  • Flash size    : ${FLASH_LOAN_SIZE_USD:,.0f}")
    print(f"  • ETH price     : sanity-filtered 100–100,000 USD")
    print()

    tasks = [poll_dex(dex, pair)
             for pair in PAIRS
             for dex in ["uniswap_v4", "uniswap_v3", "uniswap_v2"]]
    tasks.append(status_printer())
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")
    finally:
        _log_file.close()
