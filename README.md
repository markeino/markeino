# Crypto Trading Bot with Logic and Reasoning

An intelligent cryptocurrency trading bot that uses technical analysis and logical reasoning to make informed trading decisions. Built with Python and designed for both paper trading (simulation) and live trading.

## Features

- **Intelligent Strategy Engine**: Multi-factor analysis using technical indicators
- **Risk Management**: Position sizing, stop-loss, take-profit, and daily loss limits
- **Logical Reasoning**: Each trading decision includes detailed reasoning
- **Paper Trading**: Safe simulation mode to test strategies without real money
- **Live Trading**: Support for real trading on crypto exchanges (Binance, etc.)
- **Comprehensive Logging**: Track all trades, signals, and portfolio performance
- **Flexible Configuration**: Easy customization via YAML config files
- **Binance Integration**: Pre-configured for Binance exchange with WALRUS/USDC support

## Quick Start for Binance & WALRUS/USDC

Want to trade WALRUS on Binance? It's already set up!

```bash
# Install dependencies
pip install -r requirements.txt

# Run in paper trading mode (simulation - no API keys needed)
python main.py --config config.binance.yaml

# Or try the WALRUS trading example
python examples/walrus_trading.py
```

For detailed Binance setup including live trading, see [BINANCE_SETUP.md](BINANCE_SETUP.md)

## How It Works

The bot uses a sophisticated **6-factor analysis system** to generate trading signals:

1. **Trend Analysis (25%)**: EMA crossovers and trend direction
2. **Momentum Analysis (20%)**: RSI indicator for overbought/oversold conditions
3. **MACD Analysis (20%)**: Moving Average Convergence Divergence
4. **Volume Analysis (15%)**: Volume patterns and confirmation
5. **Bollinger Bands (10%)**: Price volatility and extremes
6. **Price Action (10%)**: Candlestick patterns and price movement

Each factor contributes to a confidence score. The bot only trades when confidence exceeds the configured threshold (default: 60%).

## Installation

1. Clone the repository:
```bash
git clone https://github.com/markeino/markeino.git
cd markeino
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure the bot:
```bash
cp .env.example .env
# Edit .env with your settings (optional for paper trading)
```

## Configuration

Edit `config.yaml` to customize trading parameters:

```yaml
trading:
  mode: paper  # paper or live
  initial_balance: 10000
  pairs:
    - BTC/USDT
    - ETH/USDT
  timeframe: 1h

risk_management:
  max_position_size: 0.1  # 10% per trade
  stop_loss_percentage: 0.02  # 2% stop loss
  take_profit_percentage: 0.05  # 5% take profit
  max_daily_loss: 0.05  # 5% max daily loss

strategy:
  min_confidence: 0.6  # Minimum 60% confidence to trade
```

## Usage

### Paper Trading (Recommended for Testing)

Run the bot in simulation mode:

```bash
python main.py
```

Run a single analysis cycle:

```bash
python main.py --once
```

### Live Trading (Use with Caution!)

1. Set up API credentials in `.env`:
```bash
EXCHANGE=binance
API_KEY=your_api_key
API_SECRET=your_api_secret
TRADING_MODE=live
```

2. Run the bot:
```bash
python main.py
```

## Project Structure

```
markeino/
├── bot/
│   ├── __init__.py
│   ├── config.py           # Configuration management
│   ├── executor.py         # Order execution (paper/live)
│   ├── indicators.py       # Technical indicators
│   ├── logger.py           # Logging system
│   ├── market_data.py      # Market data fetcher
│   ├── risk_manager.py     # Risk management
│   ├── strategy.py         # Trading strategy
│   └── trading_bot.py      # Main bot orchestrator
├── logs/                   # Trading logs
│   ├── trading_bot.log    # Main log file
│   ├── trades.csv         # Trade history
│   └── signals.csv        # Signal history
├── config.yaml            # Configuration file
├── .env.example           # Environment variables template
├── requirements.txt       # Python dependencies
└── main.py               # Entry point
```

## Trading Logic Example

When analyzing BTC/USDT, the bot might generate:

```
Signal: BUY BTC/USDT @ $43,250.00 (confidence: 75%)

Reasoning:
✓ Uptrend: EMA bullish crossover detected
✓ Bullish Momentum: RSI oversold at 28.5
✓ MACD Bullish: MACD bullish crossover
✓ Volume Support: High volume 2.3x average confirms move
✓ BB Oversold: Price at lower band - oversold
✓ Bullish Price Action: Strong bullish candle +1.8%

Position Details:
  Entry: $43,250.00
  Quantity: 0.017341 BTC
  Stop Loss: $42,385.00 (-2%)
  Take Profit: $45,412.50 (+5%)
```

## Risk Management

The bot includes comprehensive risk management:

- **Position Sizing**: Adjusts position size based on confidence
- **Stop Loss**: Automatically closes losing positions at -2%
- **Take Profit**: Automatically closes winning positions at +5%
- **Max Daily Loss**: Stops trading if daily loss exceeds 5%
- **Max Open Positions**: Limits simultaneous positions (default: 3)

## Logging and Monitoring

All trading activity is logged:

- **Console**: Real-time updates
- **trading_bot.log**: Detailed bot operations
- **trades.csv**: All executed trades with PnL
- **signals.csv**: All trading signals with reasoning

## Safety Features

- Paper trading mode for risk-free testing
- Comprehensive validation before executing trades
- Detailed reasoning for every decision
- Automatic position management
- Configurable risk limits

## Disclaimer

**IMPORTANT**: This bot is for educational purposes. Cryptocurrency trading carries significant risk. Always:

- Test thoroughly in paper trading mode first
- Start with small amounts in live trading
- Never invest more than you can afford to lose
- Understand the risks of automated trading
- Monitor the bot regularly

## Contributing

Contributions are welcome! Feel free to:

- Report bugs
- Suggest features
- Submit pull requests

## Contact

- Email: bendandbroken@yahoo.com
- GitHub: [@markeino](https://github.com/markeino)

## License

MIT License - feel free to use and modify for your own projects.

---

**Remember**: Past performance does not guarantee future results. Trade responsibly!
