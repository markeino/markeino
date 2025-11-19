"""
Logging and Monitoring System for Crypto Trading Bot
"""

import logging
import os
from datetime import datetime
from typing import Optional


def setup_logger(name: str = 'trading_bot', log_file: Optional[str] = None,
                level: str = 'INFO') -> logging.Logger:
    """
    Setup logging configuration

    Args:
        name: Logger name
        log_file: Path to log file
        level: Logging level

    Returns:
        Configured logger
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # Clear existing handlers
    logger.handlers = []

    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler
    if log_file:
        # Create logs directory if it doesn't exist
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger


class TradingLogger:
    """
    Enhanced logger for trading operations
    Logs trades, signals, and portfolio performance
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.trades_file = 'logs/trades.csv'
        self.signals_file = 'logs/signals.csv'

        # Create log files if they don't exist
        self._initialize_log_files()

    def _initialize_log_files(self):
        """Initialize CSV log files with headers"""
        # Create logs directory
        os.makedirs('logs', exist_ok=True)

        # Trades log
        if not os.path.exists(self.trades_file):
            with open(self.trades_file, 'w') as f:
                f.write('timestamp,symbol,side,quantity,price,pnl,balance\n')

        # Signals log
        if not os.path.exists(self.signals_file):
            with open(self.signals_file, 'w') as f:
                f.write('timestamp,symbol,action,confidence,price,reasoning\n')

    def log_signal(self, symbol: str, signal):
        """Log trading signal"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        reasoning = ' | '.join(signal.reasoning)

        # Console/file log
        self.logger.info(f"Signal: {signal.action} {symbol} @ ${signal.price:.2f} "
                        f"(confidence: {signal.confidence:.2%})")
        self.logger.info(f"Reasoning: {reasoning}")

        # CSV log
        with open(self.signals_file, 'a') as f:
            f.write(f'{timestamp},{symbol},{signal.action},{signal.confidence:.3f},'
                   f'{signal.price:.2f},"{reasoning}"\n')

    def log_trade(self, symbol: str, side: str, quantity: float,
                  price: float, pnl: float, balance: float):
        """Log executed trade"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Console/file log
        self.logger.info(f"Trade Executed: {side} {quantity:.6f} {symbol} @ ${price:.2f}")
        self.logger.info(f"PnL: ${pnl:.2f}, Balance: ${balance:.2f}")

        # CSV log
        with open(self.trades_file, 'a') as f:
            f.write(f'{timestamp},{symbol},{side},{quantity:.6f},{price:.2f},'
                   f'{pnl:.2f},{balance:.2f}\n')

    def log_portfolio_status(self, status: dict):
        """Log portfolio status"""
        self.logger.info("=" * 60)
        self.logger.info("PORTFOLIO STATUS")
        self.logger.info("=" * 60)
        self.logger.info(f"Current Balance: ${status['current_balance']:.2f}")
        self.logger.info(f"Total PnL: ${status['total_pnl']:.2f}")
        self.logger.info(f"Daily PnL: ${status['daily_pnl']:.2f}")
        self.logger.info(f"Open Positions: {status['open_positions']}")
        self.logger.info(f"Total Trades: {status['total_trades']}")
        self.logger.info(f"Win Rate: {status['win_rate']:.1f}%")
        self.logger.info("=" * 60)

    def log_position_update(self, position):
        """Log position update"""
        self.logger.info(f"Position Update: {position.symbol} | "
                        f"PnL: ${position.pnl:.2f} | "
                        f"Entry: ${position.entry_price:.2f} | "
                        f"Current: ${position.entry_price + (position.pnl/position.quantity):.2f}")
