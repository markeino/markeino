"""
Order Execution System for Crypto Trading Bot
Supports both paper trading (simulation) and live trading
"""

import logging
from typing import Dict, Optional
from datetime import datetime
import ccxt

logger = logging.getLogger(__name__)


class Order:
    """Represents a trading order"""

    def __init__(self, symbol: str, side: str, order_type: str,
                 quantity: float, price: Optional[float] = None):
        self.symbol = symbol
        self.side = side  # 'BUY' or 'SELL'
        self.order_type = order_type  # 'MARKET' or 'LIMIT'
        self.quantity = quantity
        self.price = price
        self.order_id = None
        self.status = 'PENDING'
        self.filled_quantity = 0.0
        self.filled_price = 0.0
        self.timestamp = datetime.now()
        self.execution_time = None

    def __repr__(self):
        return f"Order({self.side} {self.quantity:.6f} {self.symbol} @ {self.price})"


class PaperExecutor:
    """
    Paper trading executor (simulation)
    Simulates order execution without real money
    """

    def __init__(self):
        self.orders = []
        self.order_counter = 0
        logger.info("Initialized Paper Trading Executor (Simulation Mode)")

    def execute_order(self, order: Order, current_price: float) -> bool:
        """
        Simulate order execution

        Args:
            order: Order to execute
            current_price: Current market price

        Returns:
            True if order was filled
        """
        self.order_counter += 1
        order.order_id = f"PAPER_{self.order_counter}"

        # Simulate market order - fills immediately at current price
        if order.order_type == 'MARKET':
            order.status = 'FILLED'
            order.filled_quantity = order.quantity
            order.filled_price = current_price
            order.execution_time = datetime.now()

            self.orders.append(order)

            logger.info(f"[PAPER] Executed {order.side} order: {order.quantity:.6f} "
                       f"{order.symbol} @ ${current_price:.2f}")
            return True

        # Simulate limit order
        elif order.order_type == 'LIMIT':
            # Check if limit price is met
            if (order.side == 'BUY' and current_price <= order.price) or \
               (order.side == 'SELL' and current_price >= order.price):

                order.status = 'FILLED'
                order.filled_quantity = order.quantity
                order.filled_price = order.price
                order.execution_time = datetime.now()

                self.orders.append(order)

                logger.info(f"[PAPER] Executed LIMIT {order.side} order: "
                           f"{order.quantity:.6f} {order.symbol} @ ${order.price:.2f}")
                return True

            else:
                order.status = 'PENDING'
                self.orders.append(order)
                logger.info(f"[PAPER] LIMIT order placed but not filled yet")
                return False

        return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order"""
        for order in self.orders:
            if order.order_id == order_id and order.status == 'PENDING':
                order.status = 'CANCELLED'
                logger.info(f"[PAPER] Cancelled order {order_id}")
                return True
        return False

    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get status of an order"""
        for order in self.orders:
            if order.order_id == order_id:
                return order
        return None


class LiveExecutor:
    """
    Live trading executor
    Executes real orders on cryptocurrency exchanges
    """

    def __init__(self, exchange_name: str, api_key: str, api_secret: str):
        """
        Initialize live executor

        Args:
            exchange_name: Name of exchange (e.g., 'binance')
            api_key: API key
            api_secret: API secret
        """
        exchange_class = getattr(ccxt, exchange_name)
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })

        logger.info(f"Initialized Live Trading Executor on {exchange_name}")
        logger.warning("LIVE TRADING MODE - Real money at risk!")

    def execute_order(self, order: Order) -> bool:
        """
        Execute real order on exchange

        Args:
            order: Order to execute

        Returns:
            True if order was placed successfully
        """
        try:
            if order.order_type == 'MARKET':
                # Market order
                if order.side == 'BUY':
                    result = self.exchange.create_market_buy_order(
                        order.symbol,
                        order.quantity
                    )
                else:  # SELL
                    result = self.exchange.create_market_sell_order(
                        order.symbol,
                        order.quantity
                    )

            elif order.order_type == 'LIMIT':
                # Limit order
                if order.side == 'BUY':
                    result = self.exchange.create_limit_buy_order(
                        order.symbol,
                        order.quantity,
                        order.price
                    )
                else:  # SELL
                    result = self.exchange.create_limit_sell_order(
                        order.symbol,
                        order.quantity,
                        order.price
                    )

            # Update order with result
            order.order_id = result['id']
            order.status = result['status']
            order.execution_time = datetime.now()

            logger.info(f"[LIVE] Executed order: {order}")
            logger.info(f"Order ID: {order.order_id}, Status: {order.status}")

            return True

        except Exception as e:
            logger.error(f"[LIVE] Order execution failed: {e}")
            order.status = 'FAILED'
            return False

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order"""
        try:
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"[LIVE] Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"[LIVE] Cancel order failed: {e}")
            return False

    def get_order_status(self, order_id: str, symbol: str) -> Optional[Dict]:
        """Get status of an order"""
        try:
            order = self.exchange.fetch_order(order_id, symbol)
            return order
        except Exception as e:
            logger.error(f"[LIVE] Fetch order failed: {e}")
            return None


class TradingExecutor:
    """
    Unified trading executor that switches between paper and live trading
    """

    def __init__(self, mode: str = 'paper', exchange_name: str = 'binance',
                 api_key: Optional[str] = None, api_secret: Optional[str] = None):
        """
        Initialize trading executor

        Args:
            mode: 'paper' for simulation, 'live' for real trading
            exchange_name: Name of exchange
            api_key: API key (required for live mode)
            api_secret: API secret (required for live mode)
        """
        self.mode = mode.lower()

        if self.mode == 'paper':
            self.executor = PaperExecutor()
        elif self.mode == 'live':
            if not api_key or not api_secret:
                raise ValueError("API key and secret required for live trading")
            self.executor = LiveExecutor(exchange_name, api_key, api_secret)
        else:
            raise ValueError(f"Invalid mode: {mode}. Use 'paper' or 'live'")

        logger.info(f"Trading Executor initialized in {self.mode.upper()} mode")

    def buy(self, symbol: str, quantity: float, price: Optional[float] = None,
            order_type: str = 'MARKET') -> Optional[Order]:
        """
        Execute buy order

        Args:
            symbol: Trading pair
            quantity: Amount to buy
            price: Limit price (for LIMIT orders)
            order_type: 'MARKET' or 'LIMIT'

        Returns:
            Order object
        """
        order = Order(symbol, 'BUY', order_type, quantity, price)

        if self.mode == 'paper':
            # For paper trading, we need current price
            # This will be passed from the main bot
            return order
        else:
            success = self.executor.execute_order(order)
            return order if success else None

    def sell(self, symbol: str, quantity: float, price: Optional[float] = None,
             order_type: str = 'MARKET') -> Optional[Order]:
        """
        Execute sell order

        Args:
            symbol: Trading pair
            quantity: Amount to sell
            price: Limit price (for LIMIT orders)
            order_type: 'MARKET' or 'LIMIT'

        Returns:
            Order object
        """
        order = Order(symbol, 'SELL', order_type, quantity, price)

        if self.mode == 'paper':
            return order
        else:
            success = self.executor.execute_order(order)
            return order if success else None

    def execute_paper_order(self, order: Order, current_price: float) -> bool:
        """Execute paper order with current price"""
        if self.mode == 'paper':
            return self.executor.execute_order(order, current_price)
        return False
