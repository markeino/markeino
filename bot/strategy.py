"""
Intelligent Trading Strategy with Logic and Reasoning
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
import logging
from .indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


class TradingSignal:
    """Represents a trading signal with confidence and reasoning"""

    def __init__(self, action: str, confidence: float, price: float,
                 reasoning: List[str]):
        """
        Initialize trading signal

        Args:
            action: 'BUY', 'SELL', or 'HOLD'
            confidence: Confidence level (0.0 to 1.0)
            price: Price at which signal was generated
            reasoning: List of reasons for the signal
        """
        self.action = action
        self.confidence = confidence
        self.price = price
        self.reasoning = reasoning
        self.timestamp = pd.Timestamp.now()

    def __repr__(self):
        return f"TradingSignal(action={self.action}, confidence={self.confidence:.2f}, " \
               f"price={self.price:.2f})"


class IntelligentStrategy:
    """
    Intelligent trading strategy with multi-factor analysis and reasoning
    """

    def __init__(self, config: dict):
        """
        Initialize strategy with configuration

        Args:
            config: Strategy configuration dictionary
        """
        self.config = config
        self.indicator_config = config.get('indicators', {})
        self.min_confidence = config.get('min_confidence', 0.6)

        logger.info("Initialized Intelligent Trading Strategy")

    def analyze_market(self, df: pd.DataFrame, symbol: str) -> TradingSignal:
        """
        Analyze market data and generate trading signal with reasoning

        Args:
            df: DataFrame with OHLCV and indicators
            symbol: Trading pair being analyzed

        Returns:
            TradingSignal with action, confidence, and reasoning
        """
        # Add all technical indicators
        df = TechnicalIndicators.add_all_indicators(df, self.indicator_config)

        # Get latest values
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # Initialize scoring system
        buy_score = 0
        sell_score = 0
        reasoning = []

        # 1. Trend Analysis (EMA Crossover) - Weight: 25%
        trend_signal, trend_reason = self._analyze_trend(latest, prev)
        if trend_signal > 0:
            buy_score += 0.25
            reasoning.append(f"✓ Uptrend: {trend_reason}")
        elif trend_signal < 0:
            sell_score += 0.25
            reasoning.append(f"✓ Downtrend: {trend_reason}")

        # 2. Momentum Analysis (RSI) - Weight: 20%
        momentum_signal, momentum_reason = self._analyze_momentum(latest)
        if momentum_signal > 0:
            buy_score += 0.20
            reasoning.append(f"✓ Bullish Momentum: {momentum_reason}")
        elif momentum_signal < 0:
            sell_score += 0.20
            reasoning.append(f"✓ Bearish Momentum: {momentum_reason}")

        # 3. MACD Analysis - Weight: 20%
        macd_signal, macd_reason = self._analyze_macd(latest, prev)
        if macd_signal > 0:
            buy_score += 0.20
            reasoning.append(f"✓ MACD Bullish: {macd_reason}")
        elif macd_signal < 0:
            sell_score += 0.20
            reasoning.append(f"✓ MACD Bearish: {macd_reason}")

        # 4. Volume Analysis - Weight: 15%
        volume_signal, volume_reason = self._analyze_volume(latest)
        if volume_signal > 0:
            buy_score += 0.15
            reasoning.append(f"✓ Volume Support: {volume_reason}")
        elif volume_signal < 0:
            sell_score += 0.15
            reasoning.append(f"✓ Volume Concern: {volume_reason}")

        # 5. Bollinger Bands - Weight: 10%
        bb_signal, bb_reason = self._analyze_bollinger(latest)
        if bb_signal > 0:
            buy_score += 0.10
            reasoning.append(f"✓ BB Oversold: {bb_reason}")
        elif bb_signal < 0:
            sell_score += 0.10
            reasoning.append(f"✓ BB Overbought: {bb_reason}")

        # 6. Price Action - Weight: 10%
        price_signal, price_reason = self._analyze_price_action(latest, prev)
        if price_signal > 0:
            buy_score += 0.10
            reasoning.append(f"✓ Bullish Price Action: {price_reason}")
        elif price_signal < 0:
            sell_score += 0.10
            reasoning.append(f"✓ Bearish Price Action: {price_reason}")

        # Determine action and confidence
        if buy_score > sell_score:
            action = 'BUY'
            confidence = buy_score
        elif sell_score > buy_score:
            action = 'SELL'
            confidence = sell_score
        else:
            action = 'HOLD'
            confidence = 0.0
            reasoning.append("No clear signal - holding position")

        # Check if confidence meets minimum threshold
        if confidence < self.min_confidence:
            action = 'HOLD'
            reasoning.insert(0, f"Confidence {confidence:.2f} below threshold {self.min_confidence}")

        signal = TradingSignal(
            action=action,
            confidence=confidence,
            price=latest['close'],
            reasoning=reasoning
        )

        logger.info(f"{symbol} Signal: {signal}")
        logger.info(f"Reasoning: {reasoning}")

        return signal

    def _analyze_trend(self, latest: pd.Series, prev: pd.Series) -> Tuple[int, str]:
        """Analyze trend using EMA crossover"""
        ema_short = latest['ema_short']
        ema_long = latest['ema_long']
        prev_short = prev['ema_short']
        prev_long = prev['ema_long']

        # Bullish crossover
        if ema_short > ema_long and prev_short <= prev_long:
            return 1, "EMA bullish crossover detected"

        # Bearish crossover
        if ema_short < ema_long and prev_short >= prev_long:
            return -1, "EMA bearish crossover detected"

        # Strong uptrend
        if ema_short > ema_long and latest['close'] > ema_short:
            return 1, "Strong uptrend - price above EMAs"

        # Strong downtrend
        if ema_short < ema_long and latest['close'] < ema_short:
            return -1, "Strong downtrend - price below EMAs"

        return 0, "No clear trend"

    def _analyze_momentum(self, latest: pd.Series) -> Tuple[int, str]:
        """Analyze momentum using RSI"""
        rsi = latest['rsi']
        oversold = self.indicator_config.get('rsi_oversold', 30)
        overbought = self.indicator_config.get('rsi_overbought', 70)

        if rsi < oversold:
            return 1, f"RSI oversold at {rsi:.1f}"
        elif rsi > overbought:
            return -1, f"RSI overbought at {rsi:.1f}"
        elif 40 < rsi < 60:
            return 0, f"RSI neutral at {rsi:.1f}"
        elif rsi > 50:
            return 1, f"RSI bullish at {rsi:.1f}"
        else:
            return -1, f"RSI bearish at {rsi:.1f}"

    def _analyze_macd(self, latest: pd.Series, prev: pd.Series) -> Tuple[int, str]:
        """Analyze MACD indicator"""
        macd = latest['macd']
        signal = latest['macd_signal']
        prev_macd = prev['macd']
        prev_signal = prev['macd_signal']

        # Bullish crossover
        if macd > signal and prev_macd <= prev_signal:
            return 1, "MACD bullish crossover"

        # Bearish crossover
        if macd < signal and prev_macd >= prev_signal:
            return -1, "MACD bearish crossover"

        # Above signal line
        if macd > signal and macd > 0:
            return 1, "MACD bullish above signal"

        # Below signal line
        if macd < signal and macd < 0:
            return -1, "MACD bearish below signal"

        return 0, "MACD neutral"

    def _analyze_volume(self, latest: pd.Series) -> Tuple[int, str]:
        """Analyze volume patterns"""
        volume_ratio = latest['volume_ratio']
        threshold = self.indicator_config.get('volume_threshold', 1.5)

        if volume_ratio > threshold:
            # High volume - strengthen the signal
            return 1, f"High volume {volume_ratio:.1f}x average confirms move"
        elif volume_ratio < 0.5:
            # Low volume - weaken the signal
            return -1, f"Low volume {volume_ratio:.1f}x average - weak conviction"

        return 0, f"Normal volume at {volume_ratio:.1f}x average"

    def _analyze_bollinger(self, latest: pd.Series) -> Tuple[int, str]:
        """Analyze Bollinger Bands"""
        price = latest['close']
        bb_upper = latest['bb_upper']
        bb_lower = latest['bb_lower']
        bb_middle = latest['bb_middle']

        if price <= bb_lower:
            return 1, f"Price at lower band - oversold"
        elif price >= bb_upper:
            return -1, f"Price at upper band - overbought"
        elif price > bb_middle:
            return 1, "Price above middle band"
        elif price < bb_middle:
            return -1, "Price below middle band"

        return 0, "Price near middle band"

    def _analyze_price_action(self, latest: pd.Series, prev: pd.Series) -> Tuple[int, str]:
        """Analyze price action patterns"""
        current_close = latest['close']
        prev_close = prev['close']
        change_pct = ((current_close - prev_close) / prev_close) * 100

        # Strong bullish candle
        if latest['close'] > latest['open'] and change_pct > 1:
            return 1, f"Strong bullish candle +{change_pct:.2f}%"

        # Strong bearish candle
        if latest['close'] < latest['open'] and change_pct < -1:
            return -1, f"Strong bearish candle {change_pct:.2f}%"

        # Bullish
        if change_pct > 0:
            return 1, f"Bullish +{change_pct:.2f}%"

        # Bearish
        if change_pct < 0:
            return -1, f"Bearish {change_pct:.2f}%"

        return 0, "Neutral price action"
