mod config;
mod data;
mod engine;
mod execution;
mod monitoring;
mod risk;
mod utils;

use crate::config::AppConfig;
use crate::data::market_data_collector::MarketDataCollector;
use crate::engine::arbitrage_detector::ArbitrageDetector;
use crate::execution::exchange_connector::{BinanceConnector, ExchangeConnector, OkxConnector};
use crate::execution::order_manager::OrderManager;
use crate::monitoring::logger::init_logging;
use crate::monitoring::metrics::{Metrics, Timer};
use crate::risk::risk_manager::RiskManager;

use anyhow::{Context, Result};
use dashmap::DashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::signal;
use tracing::{error, info, warn};

#[tokio::main]
async fn main() -> Result<()> {
    // ─── Load configuration ───────────────────────────────────────────────────
    let config = AppConfig::load().unwrap_or_else(|e| {
        // If config file missing, use a safe default with paper trading enabled
        eprintln!("Config error: {}. Using built-in defaults.", e);
        build_default_config()
    });
    let config = Arc::new(config);

    // ─── Initialize logging ───────────────────────────────────────────────────
    init_logging(&config.logging)?;

    info!(
        "=== Crypto Arbitrage Bot v{} ===",
        env!("CARGO_PKG_VERSION")
    );
    info!(
        "Mode: {}",
        if config.trading.paper_trading {
            "PAPER TRADING (no real orders)"
        } else {
            "LIVE TRADING"
        }
    );
    info!(
        "Trading pairs: {}",
        config.symbols.trading_pairs.join(", ")
    );

    // ─── Initialize metrics ───────────────────────────────────────────────────
    let metrics = Metrics::new()?;

    // ─── Initialize risk manager ──────────────────────────────────────────────
    let risk_manager = Arc::new(RiskManager::new(Arc::clone(&config)));

    // ─── Initialize exchange connectors ───────────────────────────────────────
    let connectors: Arc<DashMap<String, Arc<dyn ExchangeConnector>>> =
        Arc::new(DashMap::new());

    if config.exchanges.binance.enabled {
        let conn = BinanceConnector::new(
            config.exchanges.binance.api_key.clone(),
            config.exchanges.binance.api_secret.clone(),
            config.exchanges.binance.rest_url.clone(),
            config.trading.paper_trading,
        );
        connectors.insert("binance".to_string(), Arc::new(conn) as Arc<dyn ExchangeConnector>);
        info!("Binance connector initialized");
    }

    if config.exchanges.okx.enabled {
        let passphrase = config
            .exchanges
            .okx
            .passphrase
            .clone()
            .unwrap_or_default();
        let conn = OkxConnector::new(
            config.exchanges.okx.api_key.clone(),
            config.exchanges.okx.api_secret.clone(),
            passphrase,
            config.exchanges.okx.rest_url.clone(),
            config.trading.paper_trading,
        );
        connectors.insert("okx".to_string(), Arc::new(conn) as Arc<dyn ExchangeConnector>);
        info!("OKX connector initialized");
    }

    // ─── Initialize market data collector ────────────────────────────────────
    let market_data = Arc::new(MarketDataCollector::new(Arc::clone(&config))?);
    // Subscribe before starting so we don't miss early updates
    let _market_updates = market_data.subscribe();

    // ─── Initialize order manager ─────────────────────────────────────────────
    let order_manager = Arc::new(OrderManager::new(
        Arc::clone(&connectors),
        Arc::clone(&risk_manager),
        Arc::clone(&metrics),
        config.risk.position_timeout_secs,
    ));

    // ─── Initialize arbitrage detector ────────────────────────────────────────
    let detector = Arc::new(ArbitrageDetector::new(Arc::clone(&config)));

    // ─── Start HTTP metrics server ────────────────────────────────────────────
    let metrics_port = config.server.metrics_port;
    let metrics_clone = Arc::clone(&metrics);
    tokio::spawn(async move {
        if let Err(e) = start_metrics_server(metrics_clone, metrics_port).await {
            error!("Metrics server error: {}", e);
        }
    });

    // ─── Start health check server ────────────────────────────────────────────
    let health_port = config.server.health_port;
    tokio::spawn(async move {
        if let Err(e) = start_health_server(health_port).await {
            error!("Health server error: {}", e);
        }
    });

    // ─── Start market data collection ────────────────────────────────────────
    // start() spawns individual per-exchange tasks that each handle their own
    // reconnection internally — so we only call it once.
    market_data.start().await?;

    info!("All systems initialized. Starting arbitrage scan loop...");

    // ─── Main arbitrage scan loop ─────────────────────────────────────────────
    let symbols = config.symbols.trading_pairs.clone();
    let scan_interval = Duration::from_millis(config.symbols.refresh_interval_ms);

    let main_loop = {
        let detector = Arc::clone(&detector);
        let market_data = Arc::clone(&market_data);
        let order_manager = Arc::clone(&order_manager);
        let risk_manager = Arc::clone(&risk_manager);
        let metrics = Arc::clone(&metrics);
        let config = Arc::clone(&config);

        async move {
            let mut interval = tokio::time::interval(scan_interval);

            loop {
                interval.tick().await;

                if risk_manager.is_shutdown() {
                    warn!("Emergency shutdown active. Scan loop paused.");
                    tokio::time::sleep(Duration::from_secs(5)).await;
                    continue;
                }

                // Scan each symbol for arbitrage opportunities
                for symbol in &symbols {
                    let timer = Timer::start();
                    let snapshot = market_data.snapshot(symbol);

                    if snapshot.tickers.len() < 2 {
                        continue; // Not enough data yet
                    }

                    // Quick scan first to avoid expensive full scan when no opportunity
                    if !detector.quick_scan(&snapshot) {
                        continue;
                    }

                    // Full opportunity detection
                    let opportunities = detector.detect(&snapshot);
                    let latency = timer.elapsed_ms();
                    metrics
                        .opportunity_detection_latency_ms
                        .observe(latency);

                    if opportunities.is_empty() {
                        continue;
                    }

                    metrics.record_opportunity_detected();

                    // Take the best opportunity
                    let best = &opportunities[0];

                    info!(
                        "Best opportunity for {}: Buy {} @ {:.4}, Sell {} @ {:.4}, Net: {:.4}% (${:.2})",
                        symbol,
                        best.buy_exchange,
                        best.buy_price,
                        best.sell_exchange,
                        best.sell_price,
                        best.net_profit_pct * rust_decimal_macros::dec!(100),
                        best.net_profit_usd,
                    );

                    // Check active positions limit
                    if order_manager.active_trade_count() >= config.risk.max_open_positions as usize {
                        warn!("Max open positions reached, skipping opportunity");
                        metrics.record_opportunity_rejected_risk();
                        continue;
                    }

                    // Execute (or simulate in paper trading mode)
                    let order_manager = Arc::clone(&order_manager);
                    let opp = best.clone();
                    let metrics = Arc::clone(&metrics);

                    tokio::spawn(async move {
                        match order_manager.execute_opportunity(&opp).await {
                            Ok(trade) => {
                                metrics.record_opportunity_executed();
                                info!(
                                    "Trade {} completed: {:?}",
                                    trade.id, trade.state
                                );
                            }
                            Err(e) => {
                                warn!("Trade execution failed: {}", e);
                                metrics.record_opportunity_rejected_risk();
                            }
                        }
                    });
                }
            }
        }
    };

    // ─── Graceful shutdown ────────────────────────────────────────────────────
    tokio::select! {
        _ = main_loop => {},
        _ = signal::ctrl_c() => {
            info!("Received CTRL+C, shutting down gracefully...");
        }
    }

    info!("Crypto Arbitrage Bot stopped.");
    Ok(())
}

// ─── Metrics HTTP server ──────────────────────────────────────────────────────
// Pure tokio TCP — no hyper Server, so port conflicts return Err not panic.

async fn start_metrics_server(metrics: Arc<Metrics>, port: u16) -> Result<()> {
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("Cannot bind metrics server to port {}", port))?;
    info!("Prometheus metrics available at http://0.0.0.0:{}/metrics", port);

    loop {
        let (mut stream, _) = listener.accept().await?;
        let metrics = Arc::clone(&metrics);
        tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            let n = match stream.read(&mut buf).await {
                Ok(n) => n,
                Err(_) => return,
            };
            let request = String::from_utf8_lossy(&buf[..n]);
            let (status, content_type, body) = if request.starts_with("GET /metrics") {
                ("200 OK", "text/plain; version=0.0.4", metrics.gather())
            } else {
                ("404 Not Found", "text/plain", "Not found".to_string())
            };
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                status, content_type, body.len(), body
            );
            let _ = stream.write_all(response.as_bytes()).await;
        });
    }
}

// ─── Health check HTTP server ─────────────────────────────────────────────────

async fn start_health_server(port: u16) -> Result<()> {
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("Cannot bind health server to port {}", port))?;
    info!("Health check available at http://0.0.0.0:{}/health", port);

    loop {
        let (mut stream, _) = listener.accept().await?;
        tokio::spawn(async move {
            let mut buf = [0u8; 1024];
            let n = match stream.read(&mut buf).await {
                Ok(n) => n,
                Err(_) => return,
            };
            let request = String::from_utf8_lossy(&buf[..n]);
            let (status, body) = if request.starts_with("GET /health") {
                ("200 OK", r#"{"status":"ok"}"#)
            } else {
                ("404 Not Found", "Not found")
            };
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                status, body.len(), body
            );
            let _ = stream.write_all(response.as_bytes()).await;
        });
    }
}

// ─── Default config for quick-start without config file ──────────────────────

fn build_default_config() -> AppConfig {
    use crate::config::*;
    use rust_decimal_macros::dec;

    AppConfig {
        server: ServerConfig {
            metrics_port: 9090,
            health_port: 8080,
        },
        redis: RedisConfig {
            url: "redis://127.0.0.1:6379".into(),
            cache_ttl_ms: 500,
        },
        trading: TradingConfig {
            min_profit_threshold: dec!(0.006),
            max_trade_size_usd: dec!(5000),
            max_daily_loss_usd: dec!(300),
            min_liquidity_usd: dec!(200000),
            max_slippage: dec!(0.003),
            paper_trading: true,
            base_currency: "USDT".into(),
        },
        exchanges: ExchangesConfig {
            binance: ExchangeConfig {
                enabled: true,
                ws_url: "wss://stream.binance.com:9443/ws".into(),
                rest_url: "https://api.binance.com".into(),
                api_key: std::env::var("BINANCE_API_KEY").unwrap_or_default(),
                api_secret: std::env::var("BINANCE_API_SECRET").unwrap_or_default(),
                passphrase: None,
                maker_fee: dec!(0.001),
                taker_fee: dec!(0.001),
                withdrawal_fee_eth: dec!(0.005),
            },
            okx: ExchangeConfig {
                enabled: true,
                ws_url: "wss://ws.okx.com:8443/ws/v5/public".into(),
                rest_url: "https://www.okx.com".into(),
                api_key: std::env::var("OKX_API_KEY").unwrap_or_default(),
                api_secret: std::env::var("OKX_API_SECRET").unwrap_or_default(),
                passphrase: std::env::var("OKX_PASSPHRASE").ok(),
                maker_fee: dec!(0.0008),
                taker_fee: dec!(0.001),
                withdrawal_fee_eth: dec!(0.003),
            },
            bybit: ExchangeConfig {
                enabled: true,
                ws_url: "wss://stream.bybit.com/v5/public/spot".into(),
                rest_url: "https://api.bybit.com".into(),
                api_key: std::env::var("BYBIT_API_KEY").unwrap_or_default(),
                api_secret: std::env::var("BYBIT_API_SECRET").unwrap_or_default(),
                passphrase: None,
                maker_fee: dec!(0.001),
                taker_fee: dec!(0.001),
                withdrawal_fee_eth: dec!(0.004),
            },
            kraken: ExchangeConfig {
                enabled: true,
                ws_url: "wss://ws.kraken.com".into(),
                rest_url: "https://api.kraken.com".into(),
                api_key: std::env::var("KRAKEN_API_KEY").unwrap_or_default(),
                api_secret: std::env::var("KRAKEN_API_SECRET").unwrap_or_default(),
                passphrase: None,
                maker_fee: dec!(0.0016),
                taker_fee: dec!(0.0026),
                withdrawal_fee_eth: dec!(0.0035),
            },
        },
        dex: DexConfig {
            uniswap_v3: DexExchangeConfig {
                enabled: false,
                rpc_url: std::env::var("ETH_RPC_URL").unwrap_or_default(),
                router_address: "0xE592427A0AEce92De3Edee1F18E0157C05861564".into(),
                factory_address: Some("0x1F98431c8aD98523631AE4a59f267346ea31F984".into()),
                gas_limit: Some(300000),
            },
            sushiswap: DexExchangeConfig {
                enabled: false,
                rpc_url: std::env::var("ETH_RPC_URL").unwrap_or_default(),
                router_address: "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F".into(),
                factory_address: None,
                gas_limit: None,
            },
            pancakeswap: DexExchangeConfig {
                enabled: false,
                rpc_url: "https://bsc-dataseed.binance.org".into(),
                router_address: "0x10ED43C718714eb63d5aA57B78B54704E256024E".into(),
                factory_address: None,
                gas_limit: None,
            },
        },
        symbols: SymbolsConfig {
            trading_pairs: vec![
                "ETH/USDT".into(),
                "BTC/USDT".into(),
                "BNB/USDT".into(),
                "SOL/USDT".into(),
            ],
            refresh_interval_ms: 200,
        },
        risk: RiskConfig {
            max_open_positions: 3,
            position_timeout_secs: 30,
            circuit_breaker_loss_pct: dec!(0.05),
            max_consecutive_losses: 5,
        },
        logging: LoggingConfig {
            level: "info".into(),
            file_path: "logs/arb-bot.log".into(),
            json_format: false,
        },
    }
}
