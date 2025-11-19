"""
Configuration Management for Crypto Trading Bot
"""

import yaml
import os
from typing import Dict, Any
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuration manager for trading bot"""

    def __init__(self, config_file: str = 'config.yaml'):
        """
        Load configuration from YAML file and environment variables

        Args:
            config_file: Path to configuration file
        """
        self.config_file = config_file
        self.config = self._load_config()
        self._override_from_env()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                return yaml.safe_load(f)
        else:
            # Return default configuration
            return self._default_config()

    def _default_config(self) -> Dict[str, Any]:
        """Return default configuration"""
        return {
            'trading': {
                'mode': 'paper',
                'initial_balance': 10000,
                'pairs': ['BTC/USDT', 'ETH/USDT'],
                'timeframe': '1h',
            },
            'risk_management': {
                'max_position_size': 0.1,
                'stop_loss_percentage': 0.02,
                'take_profit_percentage': 0.05,
                'max_daily_loss': 0.05,
                'max_open_positions': 3,
            },
            'strategy': {
                'name': 'intelligent_momentum',
                'indicators': {
                    'rsi_period': 14,
                    'rsi_oversold': 30,
                    'rsi_overbought': 70,
                    'ema_short': 9,
                    'ema_long': 21,
                    'volume_threshold': 1.5,
                },
                'min_confidence': 0.6,
            },
            'logging': {
                'level': 'INFO',
                'file': 'logs/trading_bot.log',
            }
        }

    def _override_from_env(self):
        """Override configuration with environment variables"""
        # Trading mode
        if os.getenv('TRADING_MODE'):
            self.config['trading']['mode'] = os.getenv('TRADING_MODE')

        # Initial balance
        if os.getenv('INITIAL_BALANCE'):
            self.config['trading']['initial_balance'] = float(os.getenv('INITIAL_BALANCE'))

        # Risk parameters
        if os.getenv('MAX_POSITION_SIZE'):
            self.config['risk_management']['max_position_size'] = float(
                os.getenv('MAX_POSITION_SIZE')
            )

        if os.getenv('STOP_LOSS_PERCENTAGE'):
            self.config['risk_management']['stop_loss_percentage'] = float(
                os.getenv('STOP_LOSS_PERCENTAGE')
            )

        if os.getenv('TAKE_PROFIT_PERCENTAGE'):
            self.config['risk_management']['take_profit_percentage'] = float(
                os.getenv('TAKE_PROFIT_PERCENTAGE')
            )

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value

        Args:
            key: Configuration key (supports dot notation, e.g., 'trading.mode')
            default: Default value if key not found

        Returns:
            Configuration value
        """
        keys = key.split('.')
        value = self.config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def get_exchange_credentials(self) -> Dict[str, str]:
        """Get exchange API credentials from environment"""
        return {
            'exchange': os.getenv('EXCHANGE', 'binance'),
            'api_key': os.getenv('API_KEY'),
            'api_secret': os.getenv('API_SECRET'),
        }

    def __getitem__(self, key: str) -> Any:
        """Allow dictionary-style access"""
        return self.get(key)

    def __repr__(self):
        return f"Config(mode={self.get('trading.mode')}, " \
               f"pairs={self.get('trading.pairs')})"
