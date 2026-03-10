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
import random
import time
from collections import defaultdict
from datetime import datetime

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

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
# Prices read on-chain via public Ethereum JSON-RPC (no API key required).
# Uniswap v3 / PancakeSwap v3: call slot0() → decode sqrtPriceX96
# SushiSwap v2: call getReserves() → decode reserve0/reserve1

# Public RPC endpoints tried in order; falls back on timeout/error.
_RPC_ENDPOINTS = [
    "https://cloudflare-eth.com",
    "https://rpc.ankr.com/eth",
    "https://eth.llamarpc.com",
    "https://ethereum.publicnode.com",
]
_rpc_idx = 0

# ABI call-data constants
_SLOT0_DATA    = "0x3850c7bd"   # slot0()        — Uniswap v3 / PancakeSwap v3
_RESERVES_DATA = "0x0902f1ac"   # getReserves()  — Uniswap v2 / SushiSwap v2

# Per-pool config: (call_data, pool_type, dec0, dec1, base_is_token1)
#   base_is_token1=True  → volatile asset is token1 (stable = token0, e.g. USDC/WETH)
#   base_is_token1=False → volatile asset is token0 (stable = token1, e.g. WBTC/USDC)
# Token ordering follows Ethereum address sort (lower address = token0).
_POOL_CFG: dict[str, tuple] = {
    # ── Uniswap v3 ──────────────────────────────────────────────────────────
    "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": (_SLOT0_DATA,    "v3", 6,  18, True ),  # USDC/WETH  0.05% → ETH price
    "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35": (_SLOT0_DATA,    "v3", 8,  6,  False),  # WBTC/USDC  0.30% → WBTC price
    "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36": (_SLOT0_DATA,    "v3", 18, 6,  False),  # WETH/USDT  0.30% → ETH price
    # ── PancakeSwap v3 ──────────────────────────────────────────────────────
    "0x1ac1a8feaaea1900c4166deeed0c11cc10669d36": (_SLOT0_DATA,    "v3", 6,  18, True ),  # USDC/WETH        → ETH price
    "0xd9e2a1a61b6e61b275cec326465d417e52c1b95c": (_SLOT0_DATA,    "v3", 8,  6,  False),  # WBTC/USDC        → WBTC price
    "0x6ca298d2983ab03aa1da7679389d955a4efee15c": (_SLOT0_DATA,    "v3", 18, 6,  False),  # WETH/USDT        → ETH price
    # ── SushiSwap v2 ────────────────────────────────────────────────────────
    "0x397ff1542f962076d0bfe58ea045ffa2d347aca0": (_RESERVES_DATA, "v2", 6,  18, True ),  # USDC/WETH        → ETH price
    "0xceff51756c56ceffca006cd410b03ffc46dd3a58": (_RESERVES_DATA, "v2", 8,  6,  False),  # WBTC/USDC        → WBTC price
    "0x06da0fd433c1a5d7a4faa01111c044910a184553": (_RESERVES_DATA, "v2", 18, 6,  False),  # WETH/USDT        → ETH price
}

# Logical pool address lookup (unchanged pool addresses, still used by FETCHERS)
_UNISWAP_POOLS = {
    "ETH/USDC":  "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
    "WBTC/USDC": "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35",
    "ETH/USDT":  "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36",
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
# Balancer and Curve require complex ABI interactions; excluded from on-chain fetching.
_BALANCER_POOLS: dict[str, str] = {}
_CURVE_POOLS:    dict[str, str] = {}

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

# ─── On-chain Price Fetchers (public Ethereum JSON-RPC) ──────────────────────

def _decode_slot0_price(hex_result: str, dec0: int, dec1: int,
                        base_is_token1: bool) -> float | None:
    """Decode sqrtPriceX96 from slot0() and return USD price of the volatile asset."""
    raw      = bytes.fromhex(hex_result[2:])
    sqrt_x96 = int.from_bytes(raw[:32], "big")
    if sqrt_x96 == 0:
        return None
    price_raw = (sqrt_x96 / 2**96) ** 2   # token1_raw / token0_raw
    if base_is_token1:
        # stable=token0 (e.g. USDC), volatile=token1 (e.g. WETH)
        # ETH_price = 10^(dec1-dec0) / price_raw
        return (10 ** (dec1 - dec0)) / price_raw
    else:
        # volatile=token0 (e.g. WBTC), stable=token1 (e.g. USDC)
        # asset_price = price_raw * 10^(dec0-dec1)
        return price_raw * (10 ** (dec0 - dec1))


def _decode_reserves_price(hex_result: str, dec0: int, dec1: int,
                           base_is_token1: bool) -> float | None:
    """Decode getReserves() and return USD price of the volatile asset."""
    raw = bytes.fromhex(hex_result[2:])
    r0  = int.from_bytes(raw[:32],  "big")
    r1  = int.from_bytes(raw[32:64], "big")
    if r0 == 0 or r1 == 0:
        return None
    if base_is_token1:
        return (r0 / r1) * (10 ** (dec1 - dec0))
    else:
        return (r1 / r0) * (10 ** (dec0 - dec1))


async def _eth_call(session: aiohttp.ClientSession,
                    to: str, data: str) -> str | None:
    global _rpc_idx
    for attempt in range(len(_RPC_ENDPOINTS)):
        rpc = _RPC_ENDPOINTS[(_rpc_idx + attempt) % len(_RPC_ENDPOINTS)]
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
            "id": 1,
        }
        try:
            async with session.post(rpc, json=payload, headers=_HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                resp = await r.json(content_type=None)
            result = resp.get("result")
            if result and result != "0x":
                return result
        except Exception:
            _rpc_idx = (_rpc_idx + 1) % len(_RPC_ENDPOINTS)
    return None


async def _fetch_onchain(session: aiohttp.ClientSession,
                         pool_address: str) -> float | None:
    cfg = _POOL_CFG.get(pool_address.lower())
    if cfg is None:
        return None
    call_data, pool_type, dec0, dec1, base_is_token1 = cfg
    result = await _eth_call(session, pool_address, call_data)
    if result is None:
        return None
    if pool_type == "v3":
        return _decode_slot0_price(result, dec0, dec1, base_is_token1)
    return _decode_reserves_price(result, dec0, dec1, base_is_token1)


async def fetch_uniswap_v3(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _UNISWAP_POOLS.get(pair)
    return await _fetch_onchain(session, addr) if addr else None


async def fetch_pancakeswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _PANCAKE_POOLS.get(pair)
    return await _fetch_onchain(session, addr) if addr else None


async def fetch_sushiswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    addr = _SUSHI_PAIRS.get(pair)
    return await _fetch_onchain(session, addr) if addr else None


async def fetch_balancer(session: aiohttp.ClientSession, pair: str) -> float | None:
    return None   # Balancer requires vault+pool-id ABI; not yet implemented


async def fetch_curve(session: aiohttp.ClientSession, pair: str) -> float | None:
    return None   # Curve requires pool-specific ABI; not yet implemented


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

    # stagger startup: spread 15 coroutines over 0–5 s to avoid rate-limit burst
    await asyncio.sleep(random.uniform(0, 5))

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
