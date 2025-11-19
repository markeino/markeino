#!/usr/bin/env python3
"""
WALRUS/USDC Trading Example
Demonstrates trading the WALRUS token on Binance
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.trading_bot import CryptoTradingBot
from bot.market_data import MarketDataFetcher
from bot.config import Config


def check_walrus_availability():
    """Check if WALRUS/USDC is available on Binance"""
    print("Checking WALRUS/USDC availability on Binance...")
    print()

    try:
        market_data = MarketDataFetcher('binance')
        price = market_data.get_current_price('WALRUS/USDC')

        if price:
            print(f"✓ WALRUS/USDC is available on Binance")
            print(f"  Current price: ${price:.4f}")
            print()

            # Get market info
            info = market_data.get_market_info('WALRUS/USDC')
            if info:
                print("Market Information:")
                print(f"  24h Volume: ${info.get('volume_24h', 0):,.2f}")
                print(f"  24h Change: {info.get('change_24h', 0):.2f}%")
                print(f"  24h High: ${info.get('high_24h', 0):.4f}")
                print(f"  24h Low: ${info.get('low_24h', 0):.4f}")
            print()
            return True

        else:
            print("✗ WALRUS/USDC not found on Binance")
            print()
            print("Possible solutions:")
            print("  1. Check if WALRUS is listed on Binance")
            print("  2. Try alternative pairs (WALRUS/USDT)")
            print("  3. Use a different exchange that supports WALRUS")
            print()
            return False

    except Exception as e:
        print(f"Error checking availability: {e}")
        print()
        print("Note: You can still run in paper trading mode for testing")
        print()
        return False


def run_walrus_bot():
    """Run the bot focused on WALRUS/USDC trading"""
    print("=" * 70)
    print("WALRUS/USDC TRADING BOT")
    print("=" * 70)
    print()

    # Check availability first
    available = check_walrus_availability()

    print("Initializing bot with Binance configuration...")
    print()

    try:
        # Use Binance-specific config
        bot = CryptoTradingBot(config_file='config.binance.yaml')

        print("Running analysis on WALRUS/USDC...")
        print()
        print("The bot will analyze market conditions and generate trading signals")
        print("based on multiple technical indicators and logical reasoning.")
        print()

        # Run one cycle
        bot.run_once()

        print()
        print("=" * 70)
        print("ANALYSIS COMPLETE")
        print("=" * 70)
        print()
        print("Review the results in:")
        print("  - logs/trading_bot.log (detailed logs)")
        print("  - logs/signals.csv (trading signals)")
        print("  - logs/trades.csv (executed trades)")
        print()

        if not available:
            print("Note: This was simulated data since WALRUS/USDC may not be")
            print("available yet. The bot will work properly once the pair is listed.")
            print()

    except Exception as e:
        print(f"Error: {e}")
        print()
        print("Make sure you have:")
        print("  1. Installed dependencies: pip install -r requirements.txt")
        print("  2. Internet connection for fetching market data")
        print()


def main():
    print()
    print("This example demonstrates trading WALRUS/USDC on Binance")
    print()
    print("IMPORTANT: This runs in PAPER TRADING mode (simulation)")
    print("No real money will be used or at risk.")
    print()

    run_walrus_bot()

    print()
    print("Next steps:")
    print("  1. Review the Binance setup guide: BINANCE_SETUP.md")
    print("  2. Customize config.binance.yaml for your preferences")
    print("  3. Test thoroughly in paper mode before going live")
    print("  4. When ready for live trading, set up API keys and use:")
    print("     python main.py --config config.binance.yaml")
    print()


if __name__ == '__main__':
    main()
