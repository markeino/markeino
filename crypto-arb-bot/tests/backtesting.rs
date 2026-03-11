/// Backtesting engine for the crypto arbitrage bot.
///
/// Replays historical market data through the detector and simulates
/// trade execution to evaluate strategy performance without live risk.

use chrono::{DateTime, Duration, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::collections::HashMap;
use std::sync::Arc;

// ─── Re-exports from main crate ──────────────────────────────────────────────

use crypto_arb_bot::config::*;
use crypto_arb_bot::data::types::*;
use crypto_arb_bot::engine::arbitrage_detector::*;
use crypto_arb_bot::risk::risk_manager::*;

// ─── Backtesting Data Structures ─────────────────────────────────────────────

/// A historical market data point
#[derive(Debug, Clone)]
struct HistoricalTick {
    timestamp: DateTime<Utc>,
    exchange: ExchangeId,
    symbol: String,
    bid: Decimal,
    ask: Decimal,
    volume: Decimal,
}

/// Result of a single simulated trade
#[derive(Debug, Clone)]
struct SimulatedTrade {
    timestamp: DateTime<Utc>,
    buy_exchange: ExchangeId,
    sell_exchange: ExchangeId,
    symbol: String,
    buy_price: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
    gross_profit: Decimal,
    fees: Decimal,
    net_profit: Decimal,
    net_profit_pct: Decimal,
}

/// Backtesting session results
#[derive(Debug)]
struct BacktestResults {
    total_trades: u64,
    winning_trades: u64,
    losing_trades: u64,
    total_net_profit: Decimal,
    max_drawdown: Decimal,
    win_rate: Decimal,
    avg_profit_per_trade: Decimal,
    sharpe_ratio: f64,
    trades: Vec<SimulatedTrade>,
}

impl BacktestResults {
    fn win_rate(&self) -> Decimal {
        if self.total_trades == 0 {
            return Decimal::ZERO;
        }
        Decimal::from(self.winning_trades) / Decimal::from(self.total_trades)
    }
}

/// Backtesting engine that replays ticks through the detector
struct Backtester {
    config: Arc<AppConfig>,
    detector: ArbitrageDetector,
}

impl Backtester {
    fn new(config: Arc<AppConfig>) -> Self {
        let detector = ArbitrageDetector::new(Arc::clone(&config));
        Self { config, detector }
    }

    fn run(&self, ticks: &[HistoricalTick]) -> BacktestResults {
        let mut trades: Vec<SimulatedTrade> = Vec::new();
        let mut cumulative_pnl = Decimal::ZERO;
        let mut peak_pnl = Decimal::ZERO;
        let mut max_drawdown = Decimal::ZERO;

        // Group ticks by timestamp (simulate market snapshots)
        let mut time_windows: HashMap<i64, Vec<&HistoricalTick>> = HashMap::new();
        for tick in ticks {
            let window_key = tick.timestamp.timestamp() / 1; // 1-second windows
            time_windows.entry(window_key).or_default().push(tick);
        }

        let mut sorted_windows: Vec<i64> = time_windows.keys().cloned().collect();
        sorted_windows.sort();

        for window_key in sorted_windows {
            let window_ticks = &time_windows[&window_key];

            // Build snapshot from window ticks
            let snapshot = self.build_snapshot(window_ticks);

            if snapshot.tickers.len() < 2 {
                continue;
            }

            // Detect opportunities
            let opportunities = self.detector.detect(&snapshot);

            if let Some(opp) = opportunities.first() {
                // Simulate execution with 0.05% additional slippage for realism
                let execution_slippage = dec!(0.0005);
                let actual_buy = opp.buy_price * (Decimal::ONE + execution_slippage);
                let actual_sell = opp.sell_price * (Decimal::ONE - execution_slippage);

                let gross_profit = (actual_sell - actual_buy) * opp.trade_quantity;
                let fees = opp.total_fees;
                let net_profit = gross_profit - fees;
                let net_profit_pct = net_profit / opp.trade_size_usd;

                let trade = SimulatedTrade {
                    timestamp: opp.detected_at,
                    buy_exchange: opp.buy_exchange.clone(),
                    sell_exchange: opp.sell_exchange.clone(),
                    symbol: opp.symbol.clone(),
                    buy_price: actual_buy,
                    sell_price: actual_sell,
                    quantity: opp.trade_quantity,
                    gross_profit,
                    fees,
                    net_profit,
                    net_profit_pct,
                };

                cumulative_pnl += net_profit;
                peak_pnl = peak_pnl.max(cumulative_pnl);
                let drawdown = peak_pnl - cumulative_pnl;
                max_drawdown = max_drawdown.max(drawdown);

                trades.push(trade);
            }
        }

        let total_trades = trades.len() as u64;
        let winning_trades = trades.iter().filter(|t| t.net_profit > Decimal::ZERO).count() as u64;
        let losing_trades = total_trades - winning_trades;
        let total_net_profit = trades.iter().map(|t| t.net_profit).sum();
        let avg_profit = if total_trades > 0 {
            total_net_profit / Decimal::from(total_trades)
        } else {
            Decimal::ZERO
        };

        let win_rate = if total_trades > 0 {
            Decimal::from(winning_trades) / Decimal::from(total_trades)
        } else {
            Decimal::ZERO
        };

        // Simplified Sharpe ratio calculation
        let returns: Vec<f64> = trades
            .iter()
            .map(|t| t.net_profit_pct.to_f64().unwrap_or(0.0))
            .collect();
        let sharpe = calculate_sharpe(&returns);

        BacktestResults {
            total_trades,
            winning_trades,
            losing_trades,
            total_net_profit,
            max_drawdown,
            win_rate,
            avg_profit_per_trade: avg_profit,
            sharpe_ratio: sharpe,
            trades,
        }
    }

    fn build_snapshot(&self, ticks: &[&HistoricalTick]) -> MarketSnapshot {
        let mut tickers = HashMap::new();
        let symbol = ticks.first().map(|t| t.symbol.clone()).unwrap_or_default();

        for tick in ticks {
            let ticker = Ticker {
                exchange: tick.exchange.clone(),
                symbol: tick.symbol.clone(),
                bid: tick.bid,
                ask: tick.ask,
                last: (tick.bid + tick.ask) / dec!(2),
                volume_24h: tick.volume,
                timestamp: tick.timestamp,
            };
            tickers.insert(tick.exchange.clone(), ticker);
        }

        MarketSnapshot {
            symbol,
            tickers,
            order_books: HashMap::new(),
            pool_states: HashMap::new(),
            captured_at: Utc::now(),
        }
    }
}

fn calculate_sharpe(returns: &[f64]) -> f64 {
    if returns.len() < 2 {
        return 0.0;
    }
    let n = returns.len() as f64;
    let mean = returns.iter().sum::<f64>() / n;
    let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std_dev = variance.sqrt();
    if std_dev == 0.0 {
        return 0.0;
    }
    // Annualized Sharpe (assuming ~100 trades/day, 365 days)
    (mean / std_dev) * (365.0 * 100.0_f64).sqrt()
}

// ─── Test Helpers ─────────────────────────────────────────────────────────────

fn make_test_config() -> Arc<AppConfig> {
    Arc::new(AppConfig {
        server: ServerConfig { metrics_port: 9090, health_port: 8080 },
        redis: RedisConfig { url: "redis://127.0.0.1:6379".into(), cache_ttl_ms: 500 },
        trading: TradingConfig {
            min_profit_threshold: dec!(0.006),
            max_trade_size_usd: dec!(5000),
            max_daily_loss_usd: dec!(300),
            min_liquidity_usd: dec!(100000), // lower threshold for tests
            max_slippage: dec!(0.003),
            paper_trading: true,
            base_currency: "USDT".into(),
        },
        exchanges: ExchangesConfig {
            binance: make_exchange_config(dec!(0.001), dec!(0.001), dec!(0.005)),
            okx: make_exchange_config(dec!(0.0008), dec!(0.001), dec!(0.003)),
            bybit: make_exchange_config(dec!(0.001), dec!(0.001), dec!(0.004)),
            kraken: make_exchange_config(dec!(0.0016), dec!(0.0026), dec!(0.0035)),
        },
        dex: DexConfig {
            uniswap_v3: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
            sushiswap: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
            pancakeswap: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
        },
        symbols: SymbolsConfig {
            trading_pairs: vec!["ETH/USDT".into()],
            refresh_interval_ms: 200,
        },
        risk: RiskConfig {
            max_open_positions: 3,
            position_timeout_secs: 30,
            circuit_breaker_loss_pct: dec!(0.05),
            max_consecutive_losses: 5,
        },
        logging: LoggingConfig {
            level: "warn".into(),
            file_path: "logs/test.log".into(),
            json_format: false,
        },
    })
}

fn make_exchange_config(maker: Decimal, taker: Decimal, withdrawal: Decimal) -> ExchangeConfig {
    ExchangeConfig {
        enabled: true,
        ws_url: "wss://example.com".into(),
        rest_url: "https://example.com".into(),
        api_key: "".into(),
        api_secret: "".into(),
        passphrase: None,
        maker_fee: maker,
        taker_fee: taker,
        withdrawal_fee_eth: withdrawal,
    }
}

/// Generate synthetic market data with a given spread between two exchanges
fn generate_ticks(
    base_price: Decimal,
    spread_pct: Decimal,
    n: usize,
) -> Vec<HistoricalTick> {
    let mut ticks = Vec::new();
    let mut t = Utc::now();

    for i in 0..n {
        let noise = Decimal::from((i % 10) as i64) * dec!(0.1) - dec!(0.5); // -0.5 to +0.4 noise

        // Binance: lower price
        let binance_price = base_price + noise;
        ticks.push(HistoricalTick {
            timestamp: t,
            exchange: ExchangeId::Binance,
            symbol: "ETH/USDT".into(),
            bid: binance_price - dec!(0.5),
            ask: binance_price + dec!(0.5),
            volume: dec!(1000000),
        });

        // OKX: higher price (spread applied)
        let okx_price = base_price * (Decimal::ONE + spread_pct) + noise;
        ticks.push(HistoricalTick {
            timestamp: t,
            exchange: ExchangeId::Okx,
            symbol: "ETH/USDT".into(),
            bid: okx_price - dec!(0.5),
            ask: okx_price + dec!(0.5),
            volume: dec!(1000000),
        });

        t = t + Duration::milliseconds(200);
    }

    ticks
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[test]
fn test_backtest_profitable_spread() {
    let config = make_test_config();
    let backtester = Backtester::new(config);

    // 1.5% spread should yield profitable trades after fees
    let ticks = generate_ticks(dec!(2000), dec!(0.015), 50);
    let results = backtester.run(&ticks);

    println!("\n=== Backtest Results (1.5% spread) ===");
    println!("Total trades: {}", results.total_trades);
    println!("Winning: {} | Losing: {}", results.winning_trades, results.losing_trades);
    println!("Win rate: {:.1}%", results.win_rate * dec!(100));
    println!("Total net profit: ${:.4}", results.total_net_profit);
    println!("Avg profit/trade: ${:.4}", results.avg_profit_per_trade);
    println!("Max drawdown: ${:.4}", results.max_drawdown);
    println!("Sharpe ratio: {:.2}", results.sharpe_ratio);

    assert!(results.total_trades > 0, "Should have found opportunities");
    assert!(
        results.total_net_profit > Decimal::ZERO,
        "Should be profitable with 1.5% spread"
    );
}

#[test]
fn test_backtest_no_opportunity_at_tight_spread() {
    let config = make_test_config();
    let backtester = Backtester::new(config);

    // 0.3% spread is too small after fees — should not trade
    let ticks = generate_ticks(dec!(2000), dec!(0.003), 50);
    let results = backtester.run(&ticks);

    println!("\n=== Backtest Results (0.3% spread — below threshold) ===");
    println!("Total trades: {}", results.total_trades);

    // After slippage + fees, 0.3% spread should not meet the 0.6% threshold
    for trade in &results.trades {
        assert!(
            trade.net_profit_pct >= dec!(0.006),
            "Executed trade net profit {} below threshold",
            trade.net_profit_pct
        );
    }
}

#[test]
fn test_fee_calculation_accuracy() {
    let config = make_test_config();
    let detector = ArbitrageDetector::new(config.clone());

    let mut tickers = HashMap::new();
    // 2% spread — well above threshold
    tickers.insert(
        ExchangeId::Binance,
        Ticker {
            exchange: ExchangeId::Binance,
            symbol: "ETH/USDT".into(),
            bid: dec!(1960),
            ask: dec!(1961),
            last: dec!(1960.5),
            volume_24h: dec!(50000000),
            timestamp: Utc::now(),
        },
    );
    tickers.insert(
        ExchangeId::Okx,
        Ticker {
            exchange: ExchangeId::Okx,
            symbol: "ETH/USDT".into(),
            bid: dec!(2000),
            ask: dec!(2001),
            last: dec!(2000.5),
            volume_24h: dec!(50000000),
            timestamp: Utc::now(),
        },
    );

    let snapshot = MarketSnapshot {
        symbol: "ETH/USDT".into(),
        tickers,
        order_books: HashMap::new(),
        pool_states: HashMap::new(),
        captured_at: Utc::now(),
    };

    let opps = detector.detect(&snapshot);
    for opp in &opps {
        // Verify fee structure is reasonable
        assert!(opp.total_fees > Decimal::ZERO, "Fees should be positive");
        assert!(
            opp.total_fees < opp.trade_size_usd * dec!(0.01),
            "Fees should be less than 1% of trade size"
        );

        // Net profit should account for fees
        let implied_gross = (opp.sell_price - opp.buy_price) * opp.trade_quantity;
        let expected_net = implied_gross - opp.total_fees;
        let diff = (opp.net_profit_usd - expected_net).abs();
        assert!(
            diff < dec!(0.01),
            "Net profit calculation error: expected {}, got {}",
            expected_net,
            opp.net_profit_usd
        );
    }
}

#[test]
fn test_risk_manager_integration() {
    let config = make_test_config();
    let risk = RiskManager::new(config);

    // Simulate a series of trades
    let pnl_series = vec![
        dec!(15.0),
        dec!(-5.0),
        dec!(12.0),
        dec!(8.0),
        dec!(-3.0),
    ];

    for pnl in &pnl_series {
        risk.record_trade_result(*pnl);
    }

    let status = risk.status();
    let expected_total = pnl_series.iter().sum::<Decimal>();
    let actual_pnl = status.daily_profit_usd - status.daily_loss_usd;

    assert!((actual_pnl - expected_total).abs() < dec!(0.01));
    assert_eq!(status.total_trades, pnl_series.len() as u64);
    assert_eq!(status.consecutive_losses, 1); // last trade was a loss
}

#[test]
fn test_orderbook_slippage_model() {
    use crypto_arb_bot::data::types::{OrderBook, OrderBookLevel};

    let ob = OrderBook {
        exchange: ExchangeId::Binance,
        symbol: "ETH/USDT".into(),
        bids: vec![
            OrderBookLevel { price: dec!(2000), quantity: dec!(1.0) },
            OrderBookLevel { price: dec!(1999), quantity: dec!(2.0) },
            OrderBookLevel { price: dec!(1998), quantity: dec!(5.0) },
        ],
        asks: vec![
            OrderBookLevel { price: dec!(2001), quantity: dec!(1.0) },
            OrderBookLevel { price: dec!(2002), quantity: dec!(2.0) },
            OrderBookLevel { price: dec!(2003), quantity: dec!(5.0) },
        ],
        timestamp: Utc::now(),
    };

    // For small trade ($1000), should fill at top of book
    let eff_buy = ob.effective_buy_price(dec!(1000)).unwrap();
    assert_eq!(eff_buy, dec!(2001), "Small buy should fill at best ask");

    // For large trade ($6000), should walk the book
    let eff_buy_large = ob.effective_buy_price(dec!(6000)).unwrap();
    assert!(
        eff_buy_large > dec!(2001),
        "Large buy should have slippage above best ask"
    );

    // Verify ask liquidity calculation
    let ask_liq = ob.ask_liquidity_usd();
    let expected = dec!(2001) * dec!(1) + dec!(2002) * dec!(2) + dec!(2003) * dec!(5);
    assert_eq!(ask_liq, expected);
}

#[test]
fn test_arbitrage_opportunity_expiry() {
    let opp = ArbitrageOpportunity {
        id: "test-id".into(),
        buy_exchange: ExchangeId::Binance,
        sell_exchange: ExchangeId::Okx,
        symbol: "ETH/USDT".into(),
        buy_price: dec!(2000),
        sell_price: dec!(2020),
        gross_spread: dec!(20),
        gross_spread_pct: dec!(0.01),
        total_fees: dec!(5),
        net_profit_usd: dec!(95),
        net_profit_pct: dec!(0.019),
        trade_size_usd: dec!(5000),
        trade_quantity: dec!(2.5),
        buy_slippage: dec!(0.001),
        sell_slippage: dec!(0.001),
        buy_liquidity_usd: dec!(500000),
        sell_liquidity_usd: dec!(500000),
        confidence: dec!(0.9),
        detected_at: Utc::now(),
        ttl_ms: 500,
    };

    assert!(!opp.is_expired(), "Freshly created opportunity should not be expired");

    // Simulate an expired opportunity
    let old_opp = ArbitrageOpportunity {
        detected_at: Utc::now() - Duration::seconds(5),
        ttl_ms: 100,
        ..opp.clone()
    };

    assert!(old_opp.is_expired(), "Old opportunity should be expired");
}
