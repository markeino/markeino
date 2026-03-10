"""
List of popular Decentralized Exchanges (DEXes)
"""

DEXES = [
    {
        "name": "Uniswap",
        "url": "https://uniswap.org",
        "chains": ["Ethereum", "Polygon", "Arbitrum", "Optimism", "Base"],
        "type": "AMM",
        "token": "UNI",
        "description": "The largest DEX by volume; pioneered the AMM model with constant product formula (x*y=k).",
    },
    {
        "name": "PancakeSwap",
        "url": "https://pancakeswap.finance",
        "chains": ["BNB Chain", "Ethereum", "Arbitrum", "zkSync"],
        "type": "AMM",
        "token": "CAKE",
        "description": "Leading DEX on BNB Chain; offers swaps, farms, pools, lottery, and NFTs.",
    },
    {
        "name": "SundaeSwap",
        "url": "https://sundaeswap.finance",
        "chains": ["Cardano"],
        "type": "AMM",
        "token": "SUNDAE",
        "description": "First major DEX on Cardano; uses an order-book hybrid AMM model.",
    },
    {
        "name": "Curve Finance",
        "url": "https://curve.fi",
        "chains": ["Ethereum", "Polygon", "Arbitrum", "Optimism", "Avalanche"],
        "type": "AMM (StableSwap)",
        "token": "CRV",
        "description": "Optimized for stablecoin and pegged-asset swaps with minimal slippage.",
    },
    {
        "name": "SushiSwap",
        "url": "https://sushi.com",
        "chains": ["Ethereum", "Polygon", "Arbitrum", "BNB Chain", "Avalanche"],
        "type": "AMM",
        "token": "SUSHI",
        "description": "Multi-chain DEX forked from Uniswap v2; offers swaps, lending (Kashi), and cross-chain bridging.",
    },
    {
        "name": "dYdX",
        "url": "https://dydx.exchange",
        "chains": ["dYdX Chain (Cosmos)", "Ethereum (v3)"],
        "type": "Order Book (Perpetuals)",
        "token": "DYDX",
        "description": "Decentralized perpetuals exchange with an on-chain order book; focuses on derivatives trading.",
    },
    {
        "name": "Balancer",
        "url": "https://balancer.fi",
        "chains": ["Ethereum", "Polygon", "Arbitrum", "Optimism", "Avalanche"],
        "type": "AMM (Weighted Pools)",
        "token": "BAL",
        "description": "Flexible AMM supporting multi-token pools with custom weights, enabling portfolio rebalancing.",
    },
    {
        "name": "Trader Joe",
        "url": "https://traderjoexyz.com",
        "chains": ["Avalanche", "Arbitrum", "BNB Chain"],
        "type": "AMM (Liquidity Book)",
        "token": "JOE",
        "description": "Leading DEX on Avalanche; introduced the Liquidity Book model for concentrated liquidity.",
    },
]


def list_dexes():
    """Print a formatted list of DEXes."""
    print(f"{'#':<4} {'Name':<15} {'Type':<25} {'Token':<8} {'Chains'}")
    print("-" * 90)
    for i, dex in enumerate(DEXES, 1):
        chains = ", ".join(dex["chains"][:3])
        if len(dex["chains"]) > 3:
            chains += f" (+{len(dex['chains']) - 3} more)"
        print(f"{i:<4} {dex['name']:<15} {dex['type']:<25} {dex['token']:<8} {chains}")
    print()


if __name__ == "__main__":
    list_dexes()
