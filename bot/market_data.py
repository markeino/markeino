"""
Market Data Fetcher for Crypto Trading Bot
Fetches real-time and historical market data from exchanges
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class MarketDataFetcher:
    """Fetches and processes market data from crypto exchanges"""

    def __init__(self, exchange_name: str = 'binance', api_key: Optional[str] = None,
                 api_secret: Optional[str] = None):
        """
        Initialize market data fetcher

        Args:
            exchange_name: Name of the exchange (default: binance)
            api_key: API key for authenticated requests
            api_secret: API secret for authenticated requests
        """
        self.exchange_name = exchange_name

        # Initialize exchange
        exchange_class = getattr(ccxt, exchange_name)
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })

        logger.info(f"Initialized {exchange_name} market data fetcher")

    def get_current_price(self, symbol: str) -> float:
        """
        Get current market price for a symbol

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')

        Returns:
            Current price as float
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return None

    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame:
        """
        Get OHLCV (Open, High, Low, Close, Volume) data

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            timeframe: Timeframe for candles (e.g., '1m', '5m', '1h', '1d')
            limit: Number of candles to fetch

        Returns:
            DataFrame with OHLCV data
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

            df = pd.DataFrame(
                ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            logger.info(f"Fetched {len(df)} candles for {symbol} ({timeframe})")
            return df

        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return pd.DataFrame()

    def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        """
        Get current orderbook for a symbol

        Args:
            symbol: Trading pair
            limit: Depth of orderbook to fetch

        Returns:
            Dictionary with bids and asks
        """
        try:
            orderbook = self.exchange.fetch_order_book(symbol, limit)
            return {
                'bids': orderbook['bids'],
                'asks': orderbook['asks'],
                'timestamp': orderbook['timestamp']
            }
        except Exception as e:
            logger.error(f"Error fetching orderbook for {symbol}: {e}")
            return {'bids': [], 'asks': [], 'timestamp': None}

    def get_market_info(self, symbol: str) -> Dict:
        """
        Get comprehensive market information

        Args:
            symbol: Trading pair

        Returns:
            Dictionary with market statistics
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)

            return {
                'symbol': symbol,
                'price': ticker['last'],
                'bid': ticker['bid'],
                'ask': ticker['ask'],
                'volume_24h': ticker['quoteVolume'],
                'change_24h': ticker['percentage'],
                'high_24h': ticker['high'],
                'low_24h': ticker['low'],
                'timestamp': ticker['timestamp']
            }
        except Exception as e:
            logger.error(f"Error fetching market info for {symbol}: {e}")
            return {}

    def calculate_average_volume(self, symbol: str, timeframe: str = '1h',
                                 periods: int = 20) -> float:
        """
        Calculate average trading volume

        Args:
            symbol: Trading pair
            timeframe: Timeframe for analysis
            periods: Number of periods for average

        Returns:
            Average volume
        """
        df = self.get_ohlcv(symbol, timeframe, limit=periods)
        if not df.empty:
            return df['volume'].mean()
        return 0.0
