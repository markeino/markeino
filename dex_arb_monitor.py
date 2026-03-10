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

# ─── Subgraph / API endpoints & pool identifiers ─────────────────────────────

# Uniswap v3 — Ethereum mainnet
_UNISWAP_URL = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
_UNISWAP_POOLS = {
    # token ordering in pool: lower address = token0
    "ETH/USDC":  "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # USDC(t0)/WETH(t1) 0.05%
    "WBTC/USDC": "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35",  # WBTC(t0)/USDC(t1) 0.30%
    "ETH/USDT":  "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36",  # USDT(t0)/WETH(t1) 0.30%
}

# PancakeSwap v3 — Ethereum mainnet
_PANCAKE_URL = "https://api.thegraph.com/subgraphs/name/pancakeswap/exchange-v3-eth"
_PANCAKE_POOLS = {
    "ETH/USDC":  "0x1ac1a8feaaea1900c4166deeed0c11cc10669d36",
    "WBTC/USDC": "0xd9e2a1a61b6e61b275cec326465d417e52c1b95c",
    "ETH/USDT":  "0x6ca298d2983ab03aa1da7679389d955a4efee15c",
}

# SushiSwap v2 — Ethereum mainnet
_SUSHI_URL = "https://api.thegraph.com/subgraphs/name/sushiswap/exchange"
_SUSHI_PAIRS = {
    "ETH/USDC":  "0x397ff1542f962076d0bfe58ea045ffa2d347aca0",  # USDC(t0)/WETH(t1)
    "WBTC/USDC": "0xceff51756c56ceffca006cd410b03ffc46dd3a58",  # USDC(t0)/WBTC(t1)
    "ETH/USDT":  "0x06da0fd433c1a5d7a4faa01111c044910a184553",  # USDT(t0)/WETH(t1)
}

# Balancer v2 — Ethereum mainnet  (32-byte pool IDs)
_BALANCER_URL = "https://api.thegraph.com/subgraphs/name/balancer-labs/balancer-v2"
_BALANCER_POOLS = {
    "ETH/USDC":  "0x96646936b91d6b9d7d0c47c496afbf3d6ec7b6f8000200000000000000000019",
    "WBTC/USDC": "0x8a819a4cabd6efcb4e5504fe8679a1abd831dd8f0002000000000000000000cd",
    "ETH/USDT":  "0x3e5fa9518ea95c3e533eb377c001702a9aacaa32000200000000000000000052",
}

# Curve — Ethereum mainnet REST API
# tricrypto2: coins = [USDT(0), WBTC(1), WETH(2)]
_CURVE_API = "https://api.curve.fi/v1/getPools/ethereum/main"
_CURVE_POOL = "tricrypto2"

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

# ─── Price Normalization Helper ───────────────────────────────────────────────
_BASE_SYMS = {"WETH", "ETH", "WBTC"}

def _price_from_pool(t0_sym: str, t1_sym: str,
                     t0_price: str, t1_price: str) -> float | None:
    """
    Uniswap-style pools:
      token0Price = amount of token1 per token0  (token1 / token0)
      token1Price = amount of token0 per token1  (token0 / token1)
    We always want USD per base (ETH or WBTC).
    """
    t0 = t0_sym.upper()
    t1 = t1_sym.upper()
    if t0 in _BASE_SYMS:
        return float(t0_price)   # USD-equivalent per base
    if t1 in _BASE_SYMS:
        return float(t1_price)   # USD-equivalent per base
    return None

# ─── Price Fetchers ───────────────────────────────────────────────────────────
_V3_POOL_QUERY = """
{
  pool(id: "%s") {
    token0 { symbol }
    token1 { symbol }
    token0Price
    token1Price
  }
}
"""

_V2_PAIR_QUERY = """
{
  pair(id: "%s") {
    token0 { symbol }
    token1 { symbol }
    token0Price
    token1Price
  }
}
"""

async def _graphql(session: aiohttp.ClientSession, url: str,
                   query: str) -> dict:
    async with session.post(url, json={"query": query},
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
        return await r.json()


async def fetch_uniswap_v3(session: aiohttp.ClientSession, pair: str) -> float | None:
    pool_id = _UNISWAP_POOLS.get(pair)
    if not pool_id:
        return None
    data = await _graphql(session, _UNISWAP_URL, _V3_POOL_QUERY % pool_id)
    p = data["data"]["pool"]
    return _price_from_pool(
        p["token0"]["symbol"], p["token1"]["symbol"],
        p["token0Price"],      p["token1Price"],
    )


async def fetch_pancakeswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    pool_id = _PANCAKE_POOLS.get(pair)
    if not pool_id:
        return None
    data = await _graphql(session, _PANCAKE_URL, _V3_POOL_QUERY % pool_id)
    p = data["data"]["pool"]
    return _price_from_pool(
        p["token0"]["symbol"], p["token1"]["symbol"],
        p["token0Price"],      p["token1Price"],
    )


async def fetch_sushiswap(session: aiohttp.ClientSession, pair: str) -> float | None:
    pair_id = _SUSHI_PAIRS.get(pair)
    if not pair_id:
        return None
    data = await _graphql(session, _SUSHI_URL, _V2_PAIR_QUERY % pair_id)
    p = data["data"]["pair"]
    return _price_from_pool(
        p["token0"]["symbol"], p["token1"]["symbol"],
        p["token0Price"],      p["token1Price"],
    )


async def fetch_balancer(session: aiohttp.ClientSession, pair: str) -> float | None:
    pool_id = _BALANCER_POOLS.get(pair)
    if not pool_id:
        return None
    query = """
    {
      pool(id: "%s") {
        tokens { symbol weight balance }
      }
    }
    """ % pool_id
    data  = await _graphql(session, _BALANCER_URL, query)
    tokens = data["data"]["pool"]["tokens"]

    base     = pair.split("/")[0].upper()
    usd_syms = {"USDC", "USDT", "DAI"}

    base_tok  = next((t for t in tokens if t["symbol"].upper() in _BASE_SYMS
                      and (base == "ETH" and t["symbol"].upper() in ("WETH", "ETH")
                           or base == t["symbol"].upper())), None)
    quote_tok = next((t for t in tokens if t["symbol"].upper() in usd_syms), None)

    if not base_tok or not quote_tok:
        return None

    b_base  = float(base_tok["balance"])
    b_quote = float(quote_tok["balance"])
    w_base  = float(base_tok["weight"])
    w_quote = float(quote_tok["weight"])

    if b_base == 0 or w_base == 0:
        return None

    # Balancer weighted AMM spot price: P = (B_quote / W_quote) / (B_base / W_base)
    return (b_quote / w_quote) / (b_base / w_base)


async def fetch_curve(session: aiohttp.ClientSession, pair: str) -> float | None:
    """
    Curve tricrypto2 coins: [USDT(0), WBTC(1), WETH(2)]
    usdPrices mirrors coins list: [1.0, ~60000, ~3000]
    """
    async with session.get(_CURVE_API, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json()

    pool = next(
        (p for p in data.get("data", {}).get("poolData", [])
         if p.get("id", "").lower() == _CURVE_POOL.lower()
         or p.get("name", "").lower().find("tricrypto2") >= 0),
        None,
    )
    if not pool:
        return None

    coins      = [c["symbol"].upper() for c in pool.get("coins", [])]
    usd_prices = pool.get("usdPrices", [])
    if not usd_prices:
        return None

    base = pair.split("/")[0].upper()
    if base == "ETH":
        idx = next((i for i, s in enumerate(coins) if s in ("WETH", "ETH")), None)
    elif base == "WBTC":
        idx = next((i for i, s in enumerate(coins) if s == "WBTC"), None)
    else:
        return None

    if idx is None or idx >= len(usd_prices):
        return None
    return float(usd_prices[idx])


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
