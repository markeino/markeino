"""
Technical Indicators for Trading Analysis
"""

import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice


class TechnicalIndicators:
    """Calculate technical indicators for trading decisions"""

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Relative Strength Index"""
        rsi = RSIIndicator(close=df['close'], window=period)
        return rsi.rsi()

    @staticmethod
    def calculate_ema(df: pd.DataFrame, period: int = 9) -> pd.Series:
        """Calculate Exponential Moving Average"""
        ema = EMAIndicator(close=df['close'], window=period)
        return ema.ema_indicator()

    @staticmethod
    def calculate_macd(df: pd.DataFrame) -> dict:
        """Calculate MACD (Moving Average Convergence Divergence)"""
        macd = MACD(close=df['close'])
        return {
            'macd': macd.macd(),
            'signal': macd.macd_signal(),
            'histogram': macd.macd_diff()
        }

    @staticmethod
    def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20,
                                  std_dev: int = 2) -> dict:
        """Calculate Bollinger Bands"""
        bb = BollingerBands(close=df['close'], window=period, window_dev=std_dev)
        return {
            'upper': bb.bollinger_hband(),
            'middle': bb.bollinger_mavg(),
            'lower': bb.bollinger_lband()
        }

    @staticmethod
    def calculate_stochastic(df: pd.DataFrame, period: int = 14) -> dict:
        """Calculate Stochastic Oscillator"""
        stoch = StochasticOscillator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=period
        )
        return {
            'k': stoch.stoch(),
            'd': stoch.stoch_signal()
        }

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range (volatility)"""
        atr = AverageTrueRange(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=period
        )
        return atr.average_true_range()

    @staticmethod
    def calculate_volume_profile(df: pd.DataFrame, periods: int = 20) -> dict:
        """Analyze volume patterns"""
        avg_volume = df['volume'].rolling(window=periods).mean()
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / avg_volume.iloc[-1] if avg_volume.iloc[-1] > 0 else 0

        return {
            'current': current_volume,
            'average': avg_volume.iloc[-1],
            'ratio': volume_ratio
        }

    @staticmethod
    def add_all_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
        """Add all technical indicators to dataframe"""
        # RSI
        df['rsi'] = TechnicalIndicators.calculate_rsi(
            df, period=config.get('rsi_period', 14)
        )

        # EMAs
        df['ema_short'] = TechnicalIndicators.calculate_ema(
            df, period=config.get('ema_short', 9)
        )
        df['ema_long'] = TechnicalIndicators.calculate_ema(
            df, period=config.get('ema_long', 21)
        )

        # MACD
        macd_data = TechnicalIndicators.calculate_macd(df)
        df['macd'] = macd_data['macd']
        df['macd_signal'] = macd_data['signal']
        df['macd_histogram'] = macd_data['histogram']

        # Bollinger Bands
        bb_data = TechnicalIndicators.calculate_bollinger_bands(df)
        df['bb_upper'] = bb_data['upper']
        df['bb_middle'] = bb_data['middle']
        df['bb_lower'] = bb_data['lower']

        # ATR for volatility
        df['atr'] = TechnicalIndicators.calculate_atr(df)

        # Volume analysis
        df['volume_avg'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_avg']

        return df
