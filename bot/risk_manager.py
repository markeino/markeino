"""
Risk Management Module for Crypto Trading Bot
Handles position sizing, stop losses, and risk controls
"""

import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class Position:
    """Represents a trading position"""

    def __init__(self, symbol: str, side: str, entry_price: float,
                 quantity: float, stop_loss: float, take_profit: float):
        self.symbol = symbol
        self.side = side  # 'LONG' or 'SHORT'
        self.entry_price = entry_price
        self.quantity = quantity
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.entry_time = datetime.now()
        self.exit_price = None
        self.exit_time = None
        self.pnl = 0.0
        self.status = 'OPEN'  # 'OPEN', 'CLOSED'

    def calculate_pnl(self, current_price: float) -> float:
        """Calculate current profit/loss"""
        if self.side == 'LONG':
            self.pnl = (current_price - self.entry_price) * self.quantity
        else:  # SHORT
            self.pnl = (self.entry_price - current_price) * self.quantity

        return self.pnl

    def check_stop_loss(self, current_price: float) -> bool:
        """Check if stop loss has been hit"""
        if self.side == 'LONG':
            return current_price <= self.stop_loss
        else:  # SHORT
            return current_price >= self.stop_loss

    def check_take_profit(self, current_price: float) -> bool:
        """Check if take profit has been hit"""
        if self.side == 'LONG':
            return current_price >= self.take_profit
        else:  # SHORT
            return current_price <= self.take_profit

    def close(self, exit_price: float):
        """Close the position"""
        self.exit_price = exit_price
        self.exit_time = datetime.now()
        self.status = 'CLOSED'
        self.calculate_pnl(exit_price)

    def __repr__(self):
        return f"Position({self.symbol}, {self.side}, entry={self.entry_price:.2f}, " \
               f"qty={self.quantity:.4f}, pnl={self.pnl:.2f})"


class RiskManager:
    """
    Manages trading risk including position sizing, stop losses,
    and portfolio risk limits
    """

    def __init__(self, config: dict, initial_balance: float):
        """
        Initialize risk manager

        Args:
            config: Risk management configuration
            initial_balance: Starting portfolio balance
        """
        self.config = config
        self.initial_balance = initial_balance
        self.current_balance = initial_balance

        # Risk parameters
        self.max_position_size = config.get('max_position_size', 0.1)  # 10% per trade
        self.stop_loss_pct = config.get('stop_loss_percentage', 0.02)  # 2%
        self.take_profit_pct = config.get('take_profit_percentage', 0.05)  # 5%
        self.max_daily_loss = config.get('max_daily_loss', 0.05)  # 5%
        self.max_open_positions = config.get('max_open_positions', 3)

        # Position tracking
        self.open_positions: Dict[str, Position] = {}
        self.closed_positions: List[Position] = []

        # Daily tracking
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.last_reset = datetime.now().date()

        logger.info(f"Risk Manager initialized with balance: ${initial_balance:.2f}")

    def calculate_position_size(self, symbol: str, price: float,
                                confidence: float) -> float:
        """
        Calculate position size based on risk management rules

        Args:
            symbol: Trading pair
            price: Current price
            confidence: Strategy confidence (0.0 to 1.0)

        Returns:
            Position size in base currency
        """
        # Base position size as percentage of portfolio
        base_size = self.current_balance * self.max_position_size

        # Adjust by confidence (higher confidence = larger position)
        adjusted_size = base_size * confidence

        # Calculate quantity
        quantity = adjusted_size / price

        logger.info(f"Position size for {symbol}: ${adjusted_size:.2f} ({quantity:.6f} units)")

        return quantity

    def calculate_stop_loss(self, entry_price: float, side: str) -> float:
        """
        Calculate stop loss price

        Args:
            entry_price: Entry price
            side: 'LONG' or 'SHORT'

        Returns:
            Stop loss price
        """
        if side == 'LONG':
            stop_loss = entry_price * (1 - self.stop_loss_pct)
        else:  # SHORT
            stop_loss = entry_price * (1 + self.stop_loss_pct)

        return stop_loss

    def calculate_take_profit(self, entry_price: float, side: str) -> float:
        """
        Calculate take profit price

        Args:
            entry_price: Entry price
            side: 'LONG' or 'SHORT'

        Returns:
            Take profit price
        """
        if side == 'LONG':
            take_profit = entry_price * (1 + self.take_profit_pct)
        else:  # SHORT
            take_profit = entry_price * (1 - self.take_profit_pct)

        return take_profit

    def can_open_position(self, symbol: str) -> tuple[bool, str]:
        """
        Check if we can open a new position

        Args:
            symbol: Trading pair

        Returns:
            Tuple of (can_trade, reason)
        """
        # Reset daily counters if new day
        self._check_daily_reset()

        # Check if already have position in this symbol
        if symbol in self.open_positions:
            return False, f"Already have open position in {symbol}"

        # Check max open positions
        if len(self.open_positions) >= self.max_open_positions:
            return False, f"Max open positions ({self.max_open_positions}) reached"

        # Check daily loss limit
        daily_loss_pct = (self.daily_pnl / self.initial_balance)
        if daily_loss_pct <= -self.max_daily_loss:
            return False, f"Daily loss limit reached: {daily_loss_pct*100:.2f}%"

        # Check if we have enough balance
        if self.current_balance <= 0:
            return False, "Insufficient balance"

        return True, "OK"

    def open_position(self, symbol: str, side: str, entry_price: float,
                     quantity: float) -> Optional[Position]:
        """
        Open a new position

        Args:
            symbol: Trading pair
            side: 'LONG' or 'SHORT'
            entry_price: Entry price
            quantity: Position size

        Returns:
            Position object or None if cannot open
        """
        can_trade, reason = self.can_open_position(symbol)

        if not can_trade:
            logger.warning(f"Cannot open position: {reason}")
            return None

        # Calculate stop loss and take profit
        stop_loss = self.calculate_stop_loss(entry_price, side)
        take_profit = self.calculate_take_profit(entry_price, side)

        # Create position
        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit
        )

        # Update tracking
        self.open_positions[symbol] = position
        self.daily_trades += 1

        # Update balance
        position_value = entry_price * quantity
        self.current_balance -= position_value

        logger.info(f"Opened position: {position}")
        logger.info(f"Stop Loss: {stop_loss:.2f}, Take Profit: {take_profit:.2f}")

        return position

    def close_position(self, symbol: str, exit_price: float, reason: str = "Manual"):
        """
        Close an open position

        Args:
            symbol: Trading pair
            exit_price: Exit price
            reason: Reason for closing
        """
        if symbol not in self.open_positions:
            logger.warning(f"No open position for {symbol}")
            return

        position = self.open_positions[symbol]
        position.close(exit_price)

        # Update balance
        position_value = exit_price * position.quantity
        self.current_balance += position_value

        # Update daily PnL
        self.daily_pnl += position.pnl

        # Move to closed positions
        self.closed_positions.append(position)
        del self.open_positions[symbol]

        logger.info(f"Closed position: {position} - Reason: {reason}")
        logger.info(f"PnL: ${position.pnl:.2f}, Balance: ${self.current_balance:.2f}")

    def check_positions(self, current_prices: Dict[str, float]):
        """
        Check all open positions for stop loss or take profit

        Args:
            current_prices: Dictionary of symbol -> current price
        """
        positions_to_close = []

        for symbol, position in self.open_positions.items():
            if symbol not in current_prices:
                continue

            current_price = current_prices[symbol]

            # Update PnL
            position.calculate_pnl(current_price)

            # Check stop loss
            if position.check_stop_loss(current_price):
                positions_to_close.append((symbol, current_price, "Stop Loss Hit"))

            # Check take profit
            elif position.check_take_profit(current_price):
                positions_to_close.append((symbol, current_price, "Take Profit Hit"))

        # Close positions that hit stop loss or take profit
        for symbol, price, reason in positions_to_close:
            self.close_position(symbol, price, reason)

    def _check_daily_reset(self):
        """Reset daily counters if new day"""
        today = datetime.now().date()
        if today > self.last_reset:
            logger.info(f"Daily reset - Previous PnL: ${self.daily_pnl:.2f}, Trades: {self.daily_trades}")
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset = today

    def get_portfolio_status(self) -> Dict:
        """Get current portfolio status"""
        total_pnl = self.daily_pnl + sum(
            pos.pnl for pos in self.open_positions.values()
        )

        return {
            'current_balance': self.current_balance,
            'initial_balance': self.initial_balance,
            'total_pnl': total_pnl,
            'daily_pnl': self.daily_pnl,
            'daily_trades': self.daily_trades,
            'open_positions': len(self.open_positions),
            'total_trades': len(self.closed_positions) + len(self.open_positions),
            'win_rate': self._calculate_win_rate()
        }

    def _calculate_win_rate(self) -> float:
        """Calculate win rate from closed positions"""
        if not self.closed_positions:
            return 0.0

        wins = sum(1 for pos in self.closed_positions if pos.pnl > 0)
        return (wins / len(self.closed_positions)) * 100
