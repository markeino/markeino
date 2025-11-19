#!/usr/bin/env python3
"""
Crypto Trading Bot - Main Entry Point
A simple but intelligent crypto trading bot with logic and reasoning
"""

import argparse
import sys
from bot.trading_bot import CryptoTradingBot


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Crypto Trading Bot with Logic and Reasoning'
    )

    parser.add_argument(
        '--config',
        type=str,
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )

    parser.add_argument(
        '--interval',
        type=int,
        default=300,
        help='Time between trading cycles in seconds (default: 300)'
    )

    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (useful for testing)'
    )

    args = parser.parse_args()

    # Initialize bot
    print("=" * 70)
    print("CRYPTO TRADING BOT")
    print("=" * 70)
    print("\nInitializing bot...\n")

    try:
        bot = CryptoTradingBot(config_file=args.config)

        if args.once:
            # Run once for testing
            print("\nRunning single analysis cycle...\n")
            bot.run_once()
            print("\nSingle cycle complete!")
        else:
            # Run continuously
            print(f"\nStarting continuous trading (interval: {args.interval}s)")
            print("Press Ctrl+C to stop\n")
            bot.run(interval=args.interval)

    except KeyboardInterrupt:
        print("\n\nBot stopped by user")
        sys.exit(0)

    except Exception as e:
        print(f"\n\nError: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
