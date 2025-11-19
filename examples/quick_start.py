#!/usr/bin/env python3
"""
Quick Start Example for Crypto Trading Bot
Demonstrates basic usage and features
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.trading_bot import CryptoTradingBot


def main():
    print("=" * 70)
    print("CRYPTO TRADING BOT - QUICK START EXAMPLE")
    print("=" * 70)
    print()
    print("This example demonstrates how to use the trading bot.")
    print()

    # Create bot instance
    print("1. Initializing bot in PAPER TRADING mode...")
    print()

    bot = CryptoTradingBot(config_file='config.yaml')

    print()
    print("2. Running a single analysis cycle...")
    print()

    # Run one cycle
    bot.run_once()

    print()
    print("=" * 70)
    print("EXAMPLE COMPLETE")
    print("=" * 70)
    print()
    print("Next steps:")
    print("- Review the logs in logs/trading_bot.log")
    print("- Check trades.csv and signals.csv for detailed history")
    print("- Customize config.yaml for your trading preferences")
    print("- Run 'python main.py' to start continuous trading")
    print()
    print("IMPORTANT: This was PAPER TRADING (simulation).")
    print("No real money was used or at risk.")
    print()


if __name__ == '__main__':
    main()
