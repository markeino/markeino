# Quick Reference Guide

## Trading WALRUS/USDC on Binance

### Paper Trading (Simulation - No Risk)
```bash
python main.py --config config.binance.yaml
```

### Live Trading (Real Money - Use Caution!)
```bash
# 1. Set up .env file with your Binance API keys
cp .env.example .env
# Edit .env: set API_KEY, API_SECRET, TRADING_MODE=live

# 2. Run the bot
python main.py --config config.binance.yaml
```

## Configuration Files

- `config.binance.yaml` - Binance-specific config with WALRUS/USDC
- `config.yaml` - General config with multiple pairs
- `.env` - API credentials and environment settings

## Trading Pairs

Currently configured:
- **WALRUS/USDC** (Primary)
- BTC/USDT (Optional)
- ETH/USDT (Optional)

Edit in `config.binance.yaml`:
```yaml
trading:
  pairs:
    - WALRUS/USDC
```

## Command Line Options

```bash
# Run continuous trading
python main.py --config config.binance.yaml

# Run single analysis cycle (for testing)
python main.py --config config.binance.yaml --once

# Custom update interval (seconds)
python main.py --config config.binance.yaml --interval 600
```

## Examples

```bash
# Quick start example
python examples/quick_start.py

# WALRUS trading example
python examples/walrus_trading.py

# Backtesting example
python examples/backtest_example.py
```

## Risk Settings

In `config.binance.yaml`:

```yaml
risk_management:
  max_position_size: 0.1      # 10% per trade
  stop_loss_percentage: 0.02  # 2% stop loss
  take_profit_percentage: 0.05 # 5% take profit
  max_daily_loss: 0.05        # 5% max daily loss
  max_open_positions: 3       # Max simultaneous positions
```

## Strategy Settings

```yaml
strategy:
  min_confidence: 0.6  # Only trade when 60%+ confident

  indicators:
    rsi_period: 14
    rsi_oversold: 30     # Buy signal
    rsi_overbought: 70   # Sell signal
    ema_short: 9
    ema_long: 21
    volume_threshold: 1.5
```

## Monitoring

### View real-time logs
```bash
tail -f logs/trading_bot.log
```

### View trades
```bash
cat logs/trades.csv
```

### View signals
```bash
cat logs/signals.csv
```

## Binance API Setup

1. Go to: https://www.binance.com/en/my/settings/api-management
2. Create new API key
3. Enable "Spot Trading" only
4. DO NOT enable withdrawals
5. Copy API Key and Secret to `.env`
6. (Optional) Whitelist your IP address

## Timeframe Options

Change in `config.binance.yaml`:
```yaml
trading:
  timeframe: 1h  # Options: 1m, 5m, 15m, 30m, 1h, 4h, 1d
```

## Trading Modes

### Paper Mode (Simulation)
- No API keys needed
- Simulates trading with virtual money
- Safe for testing strategies
- Set in `.env`: `TRADING_MODE=paper`

### Live Mode (Real Trading)
- Requires Binance API keys
- Uses real money
- Requires careful monitoring
- Set in `.env`: `TRADING_MODE=live`

## Safety Checklist

- [ ] Tested in paper mode for at least 1 week
- [ ] API key has ONLY "Spot Trading" enabled
- [ ] Withdrawal permissions are DISABLED
- [ ] IP whitelist is configured (optional but recommended)
- [ ] Starting with small test amount
- [ ] Understanding that bot can lose money
- [ ] Will monitor bot daily

## Troubleshooting

### WALRUS/USDC not found
```bash
# Check if pair is available
python -c "import ccxt; e=ccxt.binance(); m=e.load_markets(); print('WALRUS/USDC' in m)"
```

### API authentication failed
- Verify API key/secret in `.env`
- Check API permissions on Binance
- Verify IP whitelist if configured

### No trading signals
- Bot may be in HOLD mode (low confidence)
- Check logs: `tail -f logs/trading_bot.log`
- Consider lowering `min_confidence` (but not below 0.5)

### Connection errors
- Check internet connection
- Binance API may be rate-limiting
- Wait a few minutes and try again

## Key Files

```
markeino/
├── config.binance.yaml    # Binance configuration ← USE THIS
├── config.yaml            # General configuration
├── .env                   # Your API credentials
├── BINANCE_SETUP.md       # Detailed Binance guide
├── main.py                # Start here
├── examples/
│   └── walrus_trading.py  # WALRUS example
└── logs/
    ├── trading_bot.log    # Detailed logs
    ├── trades.csv         # Trade history
    └── signals.csv        # Signal history
```

## Support

- Email: bendandbroken@yahoo.com
- Detailed setup: [BINANCE_SETUP.md](BINANCE_SETUP.md)
- Main docs: [README.md](README.md)

## Disclaimer

Educational purposes only. Crypto trading is risky. You can lose money. Use at your own risk!
