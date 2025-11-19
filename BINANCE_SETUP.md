# Binance Setup Guide

This guide will help you set up the crypto trading bot to work with Binance and trade WALRUS/USDC.

## Prerequisites

1. A Binance account ([Sign up here](https://www.binance.com))
2. Python 3.8 or higher installed
3. Basic understanding of cryptocurrency trading

## Step 1: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 2: Configure for Binance

The bot is already configured to use Binance with WALRUS/USDC as the primary trading pair.

### For Paper Trading (Simulation - Recommended First)

No API keys needed! Just run:

```bash
python main.py --config config.binance.yaml
```

This will simulate trading WALRUS/USDC without using real money.

### For Live Trading (Real Money)

**WARNING**: Only use live trading after extensive testing in paper mode!

1. **Create Binance API Keys**:
   - Go to [Binance API Management](https://www.binance.com/en/my/settings/api-management)
   - Click "Create API"
   - Complete security verification
   - **IMPORTANT**: Only enable "Spot Trading" permission
   - **DO NOT** enable withdrawal permissions

2. **Configure Environment Variables**:
   ```bash
   cp .env.example .env
   ```

3. **Edit `.env` file**:
   ```bash
   EXCHANGE=binance
   API_KEY=your_actual_binance_api_key
   API_SECRET=your_actual_binance_api_secret
   TRADING_MODE=live
   ```

4. **Whitelist IP Address** (Recommended):
   - In Binance API settings, restrict API access to your IP address
   - This adds an extra layer of security

## Step 3: Verify WALRUS/USDC is Available

Before trading, verify that WALRUS/USDC is listed on Binance:

```bash
python -c "import ccxt; exchange = ccxt.binance(); markets = exchange.load_markets(); print('WALRUS/USDC' in markets)"
```

If it returns `False`, you may need to:
- Wait for Binance to list WALRUS/USDC
- Use an alternative exchange that supports WALRUS
- Change the trading pair in `config.binance.yaml`

## Step 4: Start Trading

### Paper Trading (Simulation)
```bash
# Run continuous trading simulation
python main.py --config config.binance.yaml

# Run single analysis cycle (for testing)
python main.py --config config.binance.yaml --once
```

### Live Trading (Real Money)
```bash
# Make sure .env is configured with TRADING_MODE=live
python main.py --config config.binance.yaml
```

## Configuration for WALRUS/USDC

The bot is configured in `config.binance.yaml` with:

- **Primary pair**: WALRUS/USDC
- **Timeframe**: 1 hour candles
- **Position size**: 10% of portfolio per trade
- **Stop loss**: 2% (protects against large losses)
- **Take profit**: 5% (locks in profits)
- **Confidence threshold**: 60% (only trades high-confidence signals)

### Customizing for WALRUS

Edit `config.binance.yaml` to adjust strategy:

```yaml
trading:
  pairs:
    - WALRUS/USDC  # Your main focus
  timeframe: 1h    # Change to 15m, 30m, 4h, etc.

risk_management:
  max_position_size: 0.1  # Adjust position size
  stop_loss_percentage: 0.02  # Adjust stop loss
  take_profit_percentage: 0.05  # Adjust take profit
```

## Understanding the Bot's Logic

When analyzing WALRUS/USDC, the bot will:

1. **Fetch market data** from Binance
2. **Calculate technical indicators**:
   - RSI (Relative Strength Index)
   - EMA crossovers (9 and 21 periods)
   - MACD
   - Bollinger Bands
   - Volume analysis
   - Price action

3. **Generate signal** with reasoning:
   ```
   Signal: BUY WALRUS/USDC @ $1.25 (confidence: 72%)

   Reasoning:
   ✓ Uptrend: EMA bullish crossover detected
   ✓ Bullish Momentum: RSI at 35 (recovering from oversold)
   ✓ MACD Bullish: MACD bullish crossover
   ✓ Volume Support: High volume 2.1x average
   ```

4. **Execute trade** if confidence > 60%
5. **Manage position** with automatic stop-loss and take-profit

## Monitoring Your Bot

### View Logs
```bash
tail -f logs/trading_bot.log
```

### View Trade History
```bash
cat logs/trades.csv
```

### View Signals
```bash
cat logs/signals.csv
```

## Safety Tips

1. **Always start with paper trading** - Test for at least a week
2. **Start small** - If going live, start with a small balance
3. **Monitor regularly** - Check the bot at least daily
4. **Set alerts** - Monitor your Binance account for unexpected activity
5. **Use IP whitelist** - Restrict API access to your IP only
6. **Disable withdrawals** - Never enable withdrawal permissions on API keys
7. **Review reasoning** - The bot logs why it makes each decision

## Troubleshooting

### "WALRUS/USDC not available"
- Check if Binance has listed WALRUS/USDC
- Try using Binance.US or other Binance regional platforms
- Consider alternative pairs like WALRUS/USDT

### "Insufficient balance"
- Make sure you have USDC in your Binance spot wallet
- For paper trading, this is simulated - check `initial_balance` in config

### "API authentication failed"
- Verify API key and secret are correct
- Check that API key has "Spot Trading" enabled
- Verify IP whitelist settings if configured

### "No trading signals"
- The bot may be in HOLD mode if confidence is too low
- Try adjusting `min_confidence` in config (but don't go below 0.5)
- Check that market data is being fetched properly

## Advanced Configuration

### Higher Frequency Trading
```yaml
timeframe: 15m  # Trade on 15-minute candles
```

### More Aggressive Strategy
```yaml
risk_management:
  max_position_size: 0.15  # Larger positions
  take_profit_percentage: 0.03  # Take profit sooner

strategy:
  min_confidence: 0.55  # Lower threshold (more trades)
```

### More Conservative Strategy
```yaml
risk_management:
  max_position_size: 0.05  # Smaller positions
  stop_loss_percentage: 0.01  # Tighter stop loss

strategy:
  min_confidence: 0.7  # Higher threshold (fewer, better trades)
```

## Support

- Check logs in `logs/` directory
- Review the main [README.md](README.md) for general information
- Contact: bendandbroken@yahoo.com

## Disclaimer

This bot is for educational purposes. Cryptocurrency trading carries significant risk. The bot's past performance does not guarantee future results. Always:

- Test thoroughly before live trading
- Never invest more than you can afford to lose
- Understand that the bot can lose money
- Monitor the bot regularly
- Start with small amounts

**Use at your own risk!**
