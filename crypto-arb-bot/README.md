# Crypto Arbitrage Bot

A production-grade cryptocurrency arbitrage bot written in Rust. Detects and executes arbitrage opportunities across centralized exchanges (CEX) and decentralized liquidity pools (DEX) with strict risk management and sub-second latency.

---

## Architecture

```
crypto-arb-bot/
├── src/
│   ├── config.rs               # Configuration management
│   ├── lib.rs                  # Library exports for testing
│   ├── main.rs                 # Entry point + scan loop + HTTP servers
│   ├── data/
│   │   ├── types.rs            # Core data types (Ticker, OrderBook, PoolState)
│   │   └── market_data_collector.rs  # WebSocket feeds + Redis cache
│   ├── engine/
│   │   ├── pricing_engine.rs   # Cross-exchange price normalization
│   │   └── arbitrage_detector.rs  # Spread detection + opportunity scoring
│   ├── execution/
│   │   ├── exchange_connector.rs  # Binance, OKX (signed REST API clients)
│   │   └── order_manager.rs    # Simultaneous dual-leg order execution
│   ├── risk/
│   │   └── risk_manager.rs     # All risk safeguards + circuit breakers
│   ├── monitoring/
│   │   ├── metrics.rs          # Prometheus metrics
│   │   └── logger.rs           # Structured JSON logging
│   └── utils/
│       └── helpers.rs          # Rolling stats, rate limiter, backoff
├── config/
│   └── default.toml            # Default configuration
├── tests/
│   └── backtesting.rs          # Historical data replay + strategy tests
├── monitoring/
│   └── prometheus.yml          # Prometheus scrape config
├── Cargo.toml
├── Dockerfile
└── docker-compose.yml
```

---

## Supported Exchanges

| Exchange     | Type | WebSocket | REST Orders | Notes                        |
|--------------|------|-----------|-------------|------------------------------|
| Binance      | CEX  | ✅         | ✅           | bookTicker + depth streams   |
| OKX          | CEX  | ✅         | ✅           | Tickers channel              |
| Bybit        | CEX  | ✅         | Planned     | Spot tickers                 |
| Kraken       | CEX  | ✅         | Planned     | Ticker channel               |
| Uniswap V3   | DEX  | Polling   | Planned     | AMM constant product model   |
| SushiSwap    | DEX  | Polling   | Planned     | AMM constant product model   |
| PancakeSwap  | DEX  | Polling   | Planned     | BSC AMM                      |

---

## Strategy

The bot implements **cross-exchange arbitrage**:

1. Monitor bid/ask prices across all enabled exchanges in real time
2. Calculate net profit after all costs:
   ```
   gross_spread  = sell_price - buy_price
   total_costs   = buy_taker_fee + sell_taker_fee + transfer/gas_fee + slippage
   net_profit    = gross_spread × quantity - total_costs
   ```
3. Execute only when `net_profit_pct ≥ 0.6%` (configurable)
4. Submit both buy and sell legs simultaneously to minimize exposure time

---

## Risk Controls

| Control                    | Default   | Description                                  |
|----------------------------|-----------|----------------------------------------------|
| Max trade size             | $5,000    | Per-trade USD cap                            |
| Max daily loss             | $300      | Auto-shutdown when exceeded                  |
| Min liquidity              | $200,000  | Skip illiquid markets                        |
| Max slippage               | 0.3%      | Reject if order book walk exceeds this       |
| Max open positions         | 3         | Concurrent trade limit                       |
| Max consecutive losses     | 5         | Circuit breaker on losing streak             |
| Portfolio circuit breaker  | 5%        | Emergency shutdown on portfolio drawdown     |
| Position timeout           | 30s       | Auto-cancel stuck orders                     |

---

## Quick Start

### Prerequisites

- Rust 1.75+
- Docker & Docker Compose (for full stack)
- Redis (optional — falls back to in-memory)

### Paper Trading Mode (Safe — No Real Orders)

```bash
# Clone the repo
git clone <repo-url>
cd crypto-arb-bot

# Run in paper trading mode (default)
cargo run --release

# The bot will:
# - Connect to exchange WebSocket feeds
# - Detect arbitrage opportunities
# - Log simulated trades (no real orders placed)
```

### Configuration

Copy and edit the config file:

```bash
cp config/default.toml config/local.toml
# Edit config/local.toml — set paper_trading = false for live trading
```

Or use environment variables:

```bash
export BINANCE_API_KEY="your_key"
export BINANCE_API_SECRET="your_secret"
export OKX_API_KEY="your_key"
export OKX_API_SECRET="your_secret"
export OKX_PASSPHRASE="your_passphrase"
export ARB_TRADING__PAPER_TRADING=false
```

### Docker Deployment

```bash
# Start full stack (bot + Redis + Prometheus + Grafana)
docker-compose up -d

# View logs
docker-compose logs -f arb-bot

# Access dashboards
# Grafana:    http://localhost:3000  (admin/admin)
# Prometheus: http://localhost:9091
# Metrics:    http://localhost:9090/metrics
# Health:     http://localhost:8080/health
```

---

## Monitoring

### Prometheus Metrics

| Metric                                | Type      | Description                          |
|---------------------------------------|-----------|--------------------------------------|
| `arb_trades_total`                    | Counter   | Total trades attempted               |
| `arb_trades_successful_total`         | Counter   | Trades that filled completely        |
| `arb_trades_failed_total`             | Counter   | Failed trades                        |
| `arb_daily_pnl_usd`                   | Gauge     | Rolling daily P&L                    |
| `arb_opportunity_detection_latency_ms`| Histogram | Time from data update to opportunity |
| `arb_order_submission_latency_ms`     | Histogram | Per-exchange order latency           |
| `arb_opportunities_detected_total`    | Counter   | Raw opportunities found              |
| `arb_open_positions`                  | Gauge     | Current open position count          |
| `arb_daily_loss_usd`                  | Gauge     | Accumulated daily loss               |
| `arb_best_spread_pct`                 | Gauge     | Best spread seen in last scan        |

---

## Testing

```bash
# Run all tests
cargo test

# Run only backtesting tests
cargo test --test backtesting

# Run with output (shows backtest results)
cargo test -- --nocapture

# Run with specific test
cargo test test_backtest_profitable_spread -- --nocapture
```

---

## Performance Targets

| Metric                        | Target        |
|-------------------------------|---------------|
| Price refresh interval        | 100–500ms     |
| Opportunity detection latency | < 10ms        |
| Order submission latency      | < 100ms       |
| Expected spread range         | 0.3%–2.0%     |
| Monthly return target         | 0.5%–3%       |

---

## Infrastructure

**Recommended server:**
- CPU: 8 cores
- RAM: 32 GB
- Storage: NVMe SSD
- OS: Linux (Ubuntu 22.04 / Debian 12)
- Network: Low-latency connection to exchange data centers

**Optional upgrades:**
- Dedicated Ethereum/BSC node for DEX trading
- Co-location near exchange servers (AWS Tokyo/Frankfurt)
- Kafka for event streaming and audit logging

---

## Safety Notice

> **Always start in paper trading mode** (`paper_trading = true` in config).
> Validate strategy performance for at least 2 weeks before switching to live trading.
> Never deploy more capital than you can afford to lose.
> Review all risk parameters before going live.

---

## Development Phases

- [x] Phase 1: Core infrastructure (config, project structure)
- [x] Phase 2: Market data engine (WebSocket feeds, Redis cache)
- [x] Phase 3: Arbitrage detection (spread calculation, fee accounting)
- [x] Phase 4: Execution layer (dual-leg simultaneous orders)
- [x] Phase 5: Risk management (all circuit breakers implemented)
- [x] Phase 6: Monitoring (Prometheus + Grafana + structured logging)
- [x] Phase 7: Backtesting engine
- [ ] Phase 8: DEX integration (Uniswap V3 RPC calls)
- [ ] Phase 9: Paper trading validation
- [ ] Phase 10: Live deployment with small capital
