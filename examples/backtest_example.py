#!/usr/bin/env python3
"""
Backtesting Example for Crypto Trading Bot
Shows how the strategy would have performed on historical data
"""

import sys
import os
import pandas as pd

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.config import Config
from bot.market_data import MarketDataFetcher
from bot.strategy import IntelligentStrategy


def backtest_strategy(symbol: str = 'BTC/USDT', timeframe: str = '1h',
                     periods: int = 500):
    """
    Simple backtesting example

    Args:
        symbol: Trading pair to backtest
        timeframe: Timeframe for analysis
        periods: Number of periods to analyze
    """
    print("=" * 70)
    print(f"BACKTESTING: {symbol}")
    print("=" * 70)
    print()

    # Load config
    config = Config()

    # Initialize components
    print(f"Fetching {periods} periods of {timeframe} data for {symbol}...")
    market_data = MarketDataFetcher('binance')
    df = market_data.get_ohlcv(symbol, timeframe, limit=periods)

    if df.empty:
        print("Error: Could not fetch market data")
        return

    print(f"Data fetched: {len(df)} candles")
    print(f"Period: {df.index[0]} to {df.index[-1]}")
    print()

    # Initialize strategy
    strategy = IntelligentStrategy(config.get('strategy', {}))

    # Track results
    signals = []
    buy_signals = 0
    sell_signals = 0
    hold_signals = 0

    print("Analyzing historical data...")
    print()

    # Analyze each candle (sliding window)
    window_size = 100
    for i in range(window_size, len(df)):
        # Get data window
        window_df = df.iloc[i-window_size:i].copy()

        # Generate signal
        signal = strategy.analyze_market(window_df, symbol)

        signals.append({
            'timestamp': df.index[i],
            'price': signal.price,
            'action': signal.action,
            'confidence': signal.confidence
        })

        if signal.action == 'BUY':
            buy_signals += 1
        elif signal.action == 'SELL':
            sell_signals += 1
        else:
            hold_signals += 1

    # Convert to DataFrame
    signals_df = pd.DataFrame(signals)

    # Calculate statistics
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"Total Periods Analyzed: {len(signals)}")
    print(f"BUY Signals: {buy_signals} ({buy_signals/len(signals)*100:.1f}%)")
    print(f"SELL Signals: {sell_signals} ({sell_signals/len(signals)*100:.1f}%)")
    print(f"HOLD Signals: {hold_signals} ({hold_signals/len(signals)*100:.1f}%)")
    print()

    # Show some example signals
    print("Sample BUY Signals:")
    buy_samples = signals_df[signals_df['action'] == 'BUY'].head(3)
    for _, row in buy_samples.iterrows():
        print(f"  {row['timestamp']}: BUY @ ${row['price']:.2f} "
              f"(confidence: {row['confidence']:.1%})")

    print()
    print("Sample SELL Signals:")
    sell_samples = signals_df[signals_df['action'] == 'SELL'].head(3)
    for _, row in sell_samples.iterrows():
        print(f"  {row['timestamp']}: SELL @ ${row['price']:.2f} "
              f"(confidence: {row['confidence']:.1%})")

    print()
    print("Note: This is a simple backtest showing signal generation.")
    print("For full backtesting with PnL calculation, integrate with risk management.")
    print()


if __name__ == '__main__':
    backtest_strategy()
