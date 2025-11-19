"""
Main Crypto Trading Bot Orchestrator
Coordinates all components: market data, strategy, risk management, and execution
"""

import time
import logging
from typing import Dict, List
from datetime import datetime

from .config import Config
from .logger import setup_logger, TradingLogger
from .market_data import MarketDataFetcher
from .strategy import IntelligentStrategy
from .risk_manager import RiskManager
from .executor import TradingExecutor

logger = logging.getLogger(__name__)


class CryptoTradingBot:
    """
    Main trading bot that orchestrates all components
    """

    def __init__(self, config_file: str = 'config.yaml'):
        """
        Initialize trading bot

        Args:
            config_file: Path to configuration file
        """
        # Load configuration
        self.config = Config(config_file)

        # Setup logging
        log_config = self.config.get('logging', {})
        self.logger = setup_logger(
            name='trading_bot',
            log_file=log_config.get('file', 'logs/trading_bot.log'),
            level=log_config.get('level', 'INFO')
        )
        self.trade_logger = TradingLogger(self.logger)

        # Get credentials
        credentials = self.config.get_exchange_credentials()

        # Initialize components
        self.market_data = MarketDataFetcher(
            exchange_name=credentials['exchange'],
            api_key=credentials.get('api_key'),
            api_secret=credentials.get('api_secret')
        )

        self.strategy = IntelligentStrategy(
            config=self.config.get('strategy', {})
        )

        self.risk_manager = RiskManager(
            config=self.config.get('risk_management', {}),
            initial_balance=self.config.get('trading.initial_balance', 10000)
        )

        self.executor = TradingExecutor(
            mode=self.config.get('trading.mode', 'paper'),
            exchange_name=credentials['exchange'],
            api_key=credentials.get('api_key'),
            api_secret=credentials.get('api_secret')
        )

        # Trading parameters
        self.trading_pairs = self.config.get('trading.pairs', ['BTC/USDT'])
        self.timeframe = self.config.get('trading.timeframe', '1h')
        self.running = False

        self.logger.info("=" * 70)
        self.logger.info("CRYPTO TRADING BOT INITIALIZED")
        self.logger.info("=" * 70)
        self.logger.info(f"Mode: {self.config.get('trading.mode').upper()}")
        self.logger.info(f"Trading Pairs: {self.trading_pairs}")
        self.logger.info(f"Timeframe: {self.timeframe}")
        self.logger.info(f"Initial Balance: ${self.risk_manager.initial_balance:.2f}")
        self.logger.info("=" * 70)

    def analyze_and_trade(self, symbol: str):
        """
        Analyze market and execute trades for a symbol

        Args:
            symbol: Trading pair to analyze
        """
        try:
            # Fetch market data
            df = self.market_data.get_ohlcv(symbol, self.timeframe, limit=100)

            if df.empty:
                self.logger.warning(f"No data available for {symbol}")
                return

            # Get current price
            current_price = self.market_data.get_current_price(symbol)
            if not current_price:
                self.logger.warning(f"Could not fetch current price for {symbol}")
                return

            # Analyze market and get signal
            signal = self.strategy.analyze_market(df, symbol)

            # Log signal
            self.trade_logger.log_signal(symbol, signal)

            # Execute trades based on signal
            if signal.action == 'BUY':
                self._execute_buy(symbol, current_price, signal.confidence, signal.reasoning)

            elif signal.action == 'SELL':
                self._execute_sell(symbol, current_price, signal.reasoning)

        except Exception as e:
            self.logger.error(f"Error analyzing {symbol}: {e}")

    def _execute_buy(self, symbol: str, price: float, confidence: float,
                    reasoning: List[str]):
        """
        Execute buy order

        Args:
            symbol: Trading pair
            price: Current price
            confidence: Strategy confidence
            reasoning: List of reasons for the trade
        """
        # Check if we can open position
        can_trade, reason = self.risk_manager.can_open_position(symbol)

        if not can_trade:
            self.logger.warning(f"Cannot buy {symbol}: {reason}")
            return

        # Calculate position size
        quantity = self.risk_manager.calculate_position_size(symbol, price, confidence)

        # Create and execute order
        order = self.executor.buy(symbol, quantity, order_type='MARKET')

        if order:
            # For paper trading, execute with current price
            if self.executor.mode == 'paper':
                self.executor.execute_paper_order(order, price)

            # Open position in risk manager
            position = self.risk_manager.open_position(
                symbol=symbol,
                side='LONG',
                entry_price=price,
                quantity=quantity
            )

            if position:
                self.trade_logger.log_trade(
                    symbol=symbol,
                    side='BUY',
                    quantity=quantity,
                    price=price,
                    pnl=0.0,
                    balance=self.risk_manager.current_balance
                )

                self.logger.info(f"✓ Opened LONG position in {symbol}")
                self.logger.info(f"  Entry: ${price:.2f}")
                self.logger.info(f"  Quantity: {quantity:.6f}")
                self.logger.info(f"  Stop Loss: ${position.stop_loss:.2f}")
                self.logger.info(f"  Take Profit: ${position.take_profit:.2f}")

    def _execute_sell(self, symbol: str, price: float, reasoning: List[str]):
        """
        Execute sell order (close position)

        Args:
            symbol: Trading pair
            price: Current price
            reasoning: List of reasons for the trade
        """
        # Check if we have an open position
        if symbol not in self.risk_manager.open_positions:
            self.logger.info(f"No open position to sell for {symbol}")
            return

        position = self.risk_manager.open_positions[symbol]

        # Create and execute sell order
        order = self.executor.sell(symbol, position.quantity, order_type='MARKET')

        if order:
            # For paper trading, execute with current price
            if self.executor.mode == 'paper':
                self.executor.execute_paper_order(order, price)

            # Close position in risk manager
            self.risk_manager.close_position(symbol, price, reason="Strategy Signal")

            self.trade_logger.log_trade(
                symbol=symbol,
                side='SELL',
                quantity=position.quantity,
                price=price,
                pnl=position.pnl,
                balance=self.risk_manager.current_balance
            )

            self.logger.info(f"✓ Closed position in {symbol}")
            self.logger.info(f"  Exit: ${price:.2f}")
            self.logger.info(f"  PnL: ${position.pnl:.2f}")

    def check_open_positions(self):
        """Check all open positions for stop loss or take profit"""
        if not self.risk_manager.open_positions:
            return

        # Get current prices for all open positions
        current_prices = {}
        for symbol in self.risk_manager.open_positions.keys():
            price = self.market_data.get_current_price(symbol)
            if price:
                current_prices[symbol] = price

        # Check positions
        self.risk_manager.check_positions(current_prices)

        # Log position updates
        for symbol, position in self.risk_manager.open_positions.items():
            if symbol in current_prices:
                position.calculate_pnl(current_prices[symbol])
                self.trade_logger.log_position_update(position)

    def run_once(self):
        """Run one iteration of the bot"""
        self.logger.info("\n" + "=" * 70)
        self.logger.info(f"Trading Cycle - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("=" * 70)

        # Check open positions first
        self.check_open_positions()

        # Analyze each trading pair
        for symbol in self.trading_pairs:
            self.logger.info(f"\nAnalyzing {symbol}...")
            self.analyze_and_trade(symbol)

        # Log portfolio status
        portfolio_status = self.risk_manager.get_portfolio_status()
        self.trade_logger.log_portfolio_status(portfolio_status)

    def run(self, interval: int = 300):
        """
        Run the bot continuously

        Args:
            interval: Time between iterations in seconds (default: 300 = 5 minutes)
        """
        self.running = True
        self.logger.info(f"Starting trading bot (interval: {interval}s)")

        try:
            while self.running:
                self.run_once()

                self.logger.info(f"\nWaiting {interval} seconds until next cycle...")
                time.sleep(interval)

        except KeyboardInterrupt:
            self.logger.info("\n\nBot stopped by user")
            self.stop()

        except Exception as e:
            self.logger.error(f"Bot error: {e}")
            self.stop()

    def stop(self):
        """Stop the bot"""
        self.running = False
        self.logger.info("Stopping trading bot...")

        # Close all open positions
        if self.risk_manager.open_positions:
            self.logger.info("Closing all open positions...")
            for symbol in list(self.risk_manager.open_positions.keys()):
                price = self.market_data.get_current_price(symbol)
                if price:
                    self.risk_manager.close_position(symbol, price, reason="Bot Shutdown")

        # Final portfolio status
        portfolio_status = self.risk_manager.get_portfolio_status()
        self.trade_logger.log_portfolio_status(portfolio_status)

        self.logger.info("Bot stopped successfully")
