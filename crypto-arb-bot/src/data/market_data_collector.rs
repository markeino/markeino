use crate::config::AppConfig;
use crate::data::types::{ExchangeId, MarketSnapshot, OrderBook, OrderBookLevel, PoolState, Ticker};
use anyhow::{Context, Result};
use chrono::Utc;
use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use redis::AsyncCommands;
use reqwest::Client as HttpClient;
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

/// Market update broadcast message
#[derive(Debug, Clone)]
pub struct MarketUpdate {
    pub exchange: ExchangeId,
    pub symbol: String,
    pub ticker: Option<Ticker>,
    pub order_book: Option<OrderBook>,
}

/// In-memory price cache shared across all collectors
pub type PriceCache = Arc<DashMap<(ExchangeId, String), Ticker>>;
pub type OrderBookCache = Arc<DashMap<(ExchangeId, String), OrderBook>>;
pub type PoolCache = Arc<DashMap<(ExchangeId, String), PoolState>>;

/// Central market data collector that manages all exchange connections
pub struct MarketDataCollector {
    config: Arc<AppConfig>,
    price_cache: PriceCache,
    order_book_cache: OrderBookCache,
    pool_cache: PoolCache,
    update_tx: broadcast::Sender<MarketUpdate>,
    redis_client: Option<redis::Client>,
}

impl MarketDataCollector {
    pub fn new(config: Arc<AppConfig>) -> Result<Self> {
        let (update_tx, _) = broadcast::channel(1024);

        let redis_client = redis::Client::open(config.redis.url.clone())
            .map(Some)
            .unwrap_or_else(|e| {
                warn!("Redis not available, using in-memory cache only: {}", e);
                None
            });

        Ok(Self {
            config,
            price_cache: Arc::new(DashMap::new()),
            order_book_cache: Arc::new(DashMap::new()),
            pool_cache: Arc::new(DashMap::new()),
            update_tx,
            redis_client,
        })
    }

    pub fn subscribe(&self) -> broadcast::Receiver<MarketUpdate> {
        self.update_tx.subscribe()
    }

    pub fn price_cache(&self) -> PriceCache {
        Arc::clone(&self.price_cache)
    }

    pub fn order_book_cache(&self) -> OrderBookCache {
        Arc::clone(&self.order_book_cache)
    }

    pub fn pool_cache(&self) -> PoolCache {
        Arc::clone(&self.pool_cache)
    }

    /// Build a MarketSnapshot for a given symbol from the current caches
    pub fn snapshot(&self, symbol: &str) -> MarketSnapshot {
        let mut tickers = std::collections::HashMap::new();
        let mut order_books = std::collections::HashMap::new();
        let mut pool_states = std::collections::HashMap::new();
        for entry in self.pool_cache.iter() {
            let (exchange, sym) = entry.key();
            if sym == symbol {
                pool_states.insert(exchange.clone(), entry.value().clone());
            }
        }

        for entry in self.price_cache.iter() {
            let (exchange, sym) = entry.key();
            if sym == symbol {
                tickers.insert(exchange.clone(), entry.value().clone());
            }
        }
        for entry in self.order_book_cache.iter() {
            let (exchange, sym) = entry.key();
            if sym == symbol {
                order_books.insert(exchange.clone(), entry.value().clone());
            }
        }

        MarketSnapshot {
            symbol: symbol.to_string(),
            tickers,
            order_books,
            pool_states,
            captured_at: Utc::now(),
        }
    }

    /// Start all data collection tasks.
    /// Spawns background tokio tasks and returns immediately.
    pub async fn start(&self) -> Result<()> {
        let config = Arc::clone(&self.config);
        let symbols = config.symbols.trading_pairs.clone();
        info!("Starting market data collector for {} symbols", symbols.len());

        let price_cache = Arc::clone(&self.price_cache);
        let ob_cache = Arc::clone(&self.order_book_cache);
        let pool_cache = Arc::clone(&self.pool_cache);
        let tx = self.update_tx.clone();

        let mut task_count = 0;

        // Start CEX WebSocket connections
        if config.exchanges.binance.enabled {
            let c = Arc::clone(&config);
            let p = Arc::clone(&price_cache);
            let o = Arc::clone(&ob_cache);
            let t = tx.clone();
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    if let Err(e) = Self::connect_binance(&c, &s, Arc::clone(&p), Arc::clone(&o), t.clone()).await {
                        error!("Binance WS error: {:#}. Reconnecting in 5s...", e);
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                }
            });
            task_count += 1;
        }

        if config.exchanges.okx.enabled {
            let c = Arc::clone(&config);
            let p = Arc::clone(&price_cache);
            let o = Arc::clone(&ob_cache);
            let t = tx.clone();
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    if let Err(e) = Self::connect_okx(&c, &s, Arc::clone(&p), Arc::clone(&o), t.clone()).await {
                        error!("OKX WS error: {:#}. Reconnecting in 5s...", e);
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                }
            });
            task_count += 1;
        }

        if config.exchanges.bybit.enabled {
            let c = Arc::clone(&config);
            let p = Arc::clone(&price_cache);
            let o = Arc::clone(&ob_cache);
            let t = tx.clone();
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    if let Err(e) = Self::connect_bybit(&c, &s, Arc::clone(&p), Arc::clone(&o), t.clone()).await {
                        error!("Bybit WS error: {:#}. Reconnecting in 5s...", e);
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                }
            });
            task_count += 1;
        }

        if config.exchanges.kraken.enabled {
            let c = Arc::clone(&config);
            let p = Arc::clone(&price_cache);
            let t = tx.clone();
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    if let Err(e) = Self::connect_kraken(&c, &s, Arc::clone(&p), t.clone()).await {
                        error!("Kraken WS error: {:#}. Reconnecting in 5s...", e);
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                }
            });
            task_count += 1;
        }

        // Start DEX polling
        if config.dex.uniswap_v3.enabled {
            let p = Arc::clone(&pool_cache);
            let rpc_url = config.dex.uniswap_v3.rpc_url.clone();
            let interval_ms = config.symbols.refresh_interval_ms;
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    for symbol in &s {
                        if let Err(e) = Self::poll_dex_pool(&ExchangeId::UniswapV3, symbol, &p, &rpc_url).await {
                            debug!("UniswapV3 poll error {}: {}", symbol, e);
                        }
                    }
                    tokio::time::sleep(Duration::from_millis(interval_ms)).await;
                }
            });
            task_count += 1;
        }

        if config.dex.sushiswap.enabled {
            let p = Arc::clone(&pool_cache);
            let rpc_url = config.dex.sushiswap.rpc_url.clone();
            let interval_ms = config.symbols.refresh_interval_ms;
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    for symbol in &s {
                        if let Err(e) = Self::poll_dex_pool(&ExchangeId::SushiSwap, symbol, &p, &rpc_url).await {
                            debug!("SushiSwap poll error {}: {}", symbol, e);
                        }
                    }
                    tokio::time::sleep(Duration::from_millis(interval_ms)).await;
                }
            });
            task_count += 1;
        }

        if config.dex.pancakeswap.enabled {
            let p = Arc::clone(&pool_cache);
            let rpc_url = config.dex.pancakeswap.rpc_url.clone();
            let interval_ms = config.symbols.refresh_interval_ms;
            let s = symbols.clone();
            tokio::spawn(async move {
                loop {
                    for symbol in &s {
                        if let Err(e) = Self::poll_dex_pool(&ExchangeId::PancakeSwap, symbol, &p, &rpc_url).await {
                            debug!("PancakeSwap poll error {}: {}", symbol, e);
                        }
                    }
                    tokio::time::sleep(Duration::from_millis(interval_ms)).await;
                }
            });
            task_count += 1;
        }

        info!("Started {} data collection tasks", task_count);
        Ok(())
    }

    // ─── Binance WebSocket ──────────────────────────────────────────────────

    async fn connect_binance(
        config: &AppConfig,
        symbols: &[String],
        price_cache: PriceCache,
        ob_cache: OrderBookCache,
        tx: broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        // Build combined stream URL: <symbol>@bookTicker for best bid/ask
        let streams: Vec<String> = symbols
            .iter()
            .flat_map(|s| {
                let normalized = s.replace('/', "").to_lowercase();
                vec![
                    format!("{}@bookTicker", normalized),
                    format!("{}@depth5@100ms", normalized),
                ]
            })
            .collect();

        let stream_path = streams.join("/");
        let url = format!(
            "{}/stream?streams={}",
            config.exchanges.binance.ws_url, stream_path
        );

        info!("Connecting to Binance WebSocket: {}", url);
        let (ws_stream, _) = connect_async(&url)
            .await
            .context("Failed to connect to Binance WebSocket")?;
        let (_, mut read) = ws_stream.split();

        info!("Connected to Binance WebSocket");

        while let Some(msg) = read.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    if let Err(e) = Self::process_binance_message(
                        &text,
                        &price_cache,
                        &ob_cache,
                        &tx,
                    ) {
                        debug!("Error processing Binance message: {}", e);
                    }
                }
                Ok(Message::Ping(_)) => {}
                Ok(Message::Close(_)) => {
                    warn!("Binance WebSocket closed");
                    break;
                }
                Err(e) => {
                    return Err(e.into());
                }
                _ => {}
            }
        }

        Ok(())
    }

    fn process_binance_message(
        text: &str,
        price_cache: &PriceCache,
        ob_cache: &OrderBookCache,
        tx: &broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let v: Value = serde_json::from_str(text)?;
        let stream = v["stream"].as_str().unwrap_or("");
        let data = &v["data"];

        if stream.ends_with("@bookTicker") {
            // Best bid/ask ticker update
            let symbol_raw = data["s"].as_str().unwrap_or("");
            let symbol = normalize_binance_symbol(symbol_raw);
            let bid = parse_decimal(&data["b"])?;
            let ask = parse_decimal(&data["a"])?;

            let ticker = Ticker {
                exchange: ExchangeId::Binance,
                symbol: symbol.clone(),
                bid,
                ask,
                last: (bid + ask) / Decimal::from(2),
                volume_24h: Decimal::ZERO,
                timestamp: Utc::now(),
            };

            price_cache.insert((ExchangeId::Binance, symbol.clone()), ticker.clone());

            let _ = tx.send(MarketUpdate {
                exchange: ExchangeId::Binance,
                symbol,
                ticker: Some(ticker),
                order_book: None,
            });
        } else if stream.ends_with("@depth5@100ms") {
            // Order book depth update
            let symbol_raw = stream
                .split('@')
                .next()
                .unwrap_or("")
                .to_uppercase();
            let symbol = normalize_binance_symbol(&symbol_raw);

            let bids = parse_binance_levels(&data["bids"])?;
            let asks = parse_binance_levels(&data["asks"])?;

            let order_book = OrderBook {
                exchange: ExchangeId::Binance,
                symbol: symbol.clone(),
                bids,
                asks,
                timestamp: Utc::now(),
            };

            ob_cache.insert((ExchangeId::Binance, symbol.clone()), order_book.clone());

            let _ = tx.send(MarketUpdate {
                exchange: ExchangeId::Binance,
                symbol,
                ticker: None,
                order_book: Some(order_book),
            });
        }

        Ok(())
    }

    // ─── OKX WebSocket ──────────────────────────────────────────────────────

    async fn connect_okx(
        config: &AppConfig,
        symbols: &[String],
        price_cache: PriceCache,
        _ob_cache: OrderBookCache,
        tx: broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let url = &config.exchanges.okx.ws_url;
        info!("Connecting to OKX WebSocket: {}", url);

        let (ws_stream, _) = connect_async(url)
            .await
            .context("Failed to connect to OKX WebSocket")?;
        let (mut write, mut read) = ws_stream.split();

        // Subscribe to ticker and orderbook channels
        let inst_ids: Vec<Value> = symbols
            .iter()
            .map(|s| {
                let inst_id = s.replace('/', "-");
                json!({"channel": "tickers", "instId": inst_id})
            })
            .collect();

        let subscribe_msg = json!({
            "op": "subscribe",
            "args": inst_ids
        });

        write.send(Message::Text(subscribe_msg.to_string())).await?;
        info!("Subscribed to OKX channels");

        while let Some(msg) = read.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    if let Err(e) = Self::process_okx_message(&text, &price_cache, &tx) {
                        debug!("Error processing OKX message: {}", e);
                    }
                }
                Ok(Message::Close(_)) => {
                    warn!("OKX WebSocket closed");
                    break;
                }
                Err(e) => return Err(e.into()),
                _ => {}
            }
        }

        Ok(())
    }

    fn process_okx_message(
        text: &str,
        price_cache: &PriceCache,
        tx: &broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let v: Value = serde_json::from_str(text)?;

        if v["event"].as_str() == Some("subscribe") {
            return Ok(()); // subscription confirmation
        }

        let channel = v["arg"]["channel"].as_str().unwrap_or("");
        if channel == "tickers" {
            if let Some(data_arr) = v["data"].as_array() {
                for data in data_arr {
                    let inst_id = data["instId"].as_str().unwrap_or("");
                    let symbol = inst_id.replace('-', "/");
                    let bid = parse_decimal(&data["bidPx"])?;
                    let ask = parse_decimal(&data["askPx"])?;
                    let last = parse_decimal(&data["last"])?;
                    let vol_24h = parse_decimal_opt(&data["vol24h"]).unwrap_or(Decimal::ZERO);

                    let ticker = Ticker {
                        exchange: ExchangeId::Okx,
                        symbol: symbol.clone(),
                        bid,
                        ask,
                        last,
                        volume_24h: vol_24h,
                        timestamp: Utc::now(),
                    };

                    price_cache.insert((ExchangeId::Okx, symbol.clone()), ticker.clone());
                    let _ = tx.send(MarketUpdate {
                        exchange: ExchangeId::Okx,
                        symbol,
                        ticker: Some(ticker),
                        order_book: None,
                    });
                }
            }
        }

        Ok(())
    }

    // ─── Bybit WebSocket ────────────────────────────────────────────────────

    async fn connect_bybit(
        config: &AppConfig,
        symbols: &[String],
        price_cache: PriceCache,
        _ob_cache: OrderBookCache,
        tx: broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let url = &config.exchanges.bybit.ws_url;
        info!("Connecting to Bybit WebSocket: {}", url);

        let (ws_stream, _) = connect_async(url)
            .await
            .context("Failed to connect to Bybit WebSocket")?;
        let (mut write, mut read) = ws_stream.split();

        // Subscribe to tickers
        let args: Vec<String> = symbols
            .iter()
            .map(|s| format!("tickers.{}", s.replace('/', "")))
            .collect();

        let subscribe_msg = json!({
            "op": "subscribe",
            "args": args
        });

        write.send(Message::Text(subscribe_msg.to_string())).await?;

        // Keep-alive ping every 20 seconds
        let mut ping_interval = tokio::time::interval(Duration::from_secs(20));

        loop {
            tokio::select! {
                msg = read.next() => {
                    match msg {
                        Some(Ok(Message::Text(text))) => {
                            if let Err(e) = Self::process_bybit_message(&text, &price_cache, &tx) {
                                debug!("Bybit message error: {}", e);
                            }
                        }
                        Some(Ok(Message::Close(_))) => {
                            warn!("Bybit WebSocket closed");
                            break;
                        }
                        Some(Err(e)) => return Err(e.into()),
                        None => break,
                        _ => {}
                    }
                }
                _ = ping_interval.tick() => {
                    let _ = write.send(Message::Text(r#"{"op":"ping"}"#.to_string())).await;
                }
            }
        }

        Ok(())
    }

    fn process_bybit_message(
        text: &str,
        price_cache: &PriceCache,
        tx: &broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let v: Value = serde_json::from_str(text)?;

        if v["op"].as_str() == Some("pong") || v["op"].as_str() == Some("subscribe") {
            return Ok(());
        }

        let topic = v["topic"].as_str().unwrap_or("");
        if topic.starts_with("tickers.") {
            let data = &v["data"];
            let symbol_raw = data["symbol"].as_str().unwrap_or("");
            let symbol = normalize_bybit_symbol(symbol_raw);

            let bid = parse_decimal_opt(&data["bid1Price"]).unwrap_or(Decimal::ZERO);
            let ask = parse_decimal_opt(&data["ask1Price"]).unwrap_or(Decimal::ZERO);
            let last = parse_decimal_opt(&data["lastPrice"]).unwrap_or(Decimal::ZERO);

            if bid > Decimal::ZERO && ask > Decimal::ZERO {
                let ticker = Ticker {
                    exchange: ExchangeId::Bybit,
                    symbol: symbol.clone(),
                    bid,
                    ask,
                    last,
                    volume_24h: Decimal::ZERO,
                    timestamp: Utc::now(),
                };

                price_cache.insert((ExchangeId::Bybit, symbol.clone()), ticker.clone());
                let _ = tx.send(MarketUpdate {
                    exchange: ExchangeId::Bybit,
                    symbol,
                    ticker: Some(ticker),
                    order_book: None,
                });
            }
        }

        Ok(())
    }

    // ─── Kraken WebSocket ───────────────────────────────────────────────────

    async fn connect_kraken(
        config: &AppConfig,
        symbols: &[String],
        price_cache: PriceCache,
        tx: broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let url = &config.exchanges.kraken.ws_url;
        info!("Connecting to Kraken WebSocket: {}", url);

        let (ws_stream, _) = connect_async(url)
            .await
            .context("Failed to connect to Kraken WebSocket")?;
        let (mut write, mut read) = ws_stream.split();

        // Kraken uses XBT instead of BTC, and different pair format
        let pairs: Vec<Value> = symbols
            .iter()
            .map(|s| {
                let pair = s.replace("BTC/", "XBT/");
                json!(pair)
            })
            .collect();

        let subscribe_msg = json!({
            "event": "subscribe",
            "pair": pairs,
            "subscription": {
                "name": "ticker"
            }
        });

        write.send(Message::Text(subscribe_msg.to_string())).await?;

        while let Some(msg) = read.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    if let Err(e) = Self::process_kraken_message(&text, &price_cache, &tx) {
                        debug!("Kraken message error: {}", e);
                    }
                }
                Ok(Message::Close(_)) => {
                    warn!("Kraken WebSocket closed");
                    break;
                }
                Err(e) => return Err(e.into()),
                _ => {}
            }
        }

        Ok(())
    }

    fn process_kraken_message(
        text: &str,
        price_cache: &PriceCache,
        tx: &broadcast::Sender<MarketUpdate>,
    ) -> Result<()> {
        let v: Value = serde_json::from_str(text)?;

        // Kraken ticker messages are arrays: [channelID, data, "ticker", "PAIR"]
        if let Some(arr) = v.as_array() {
            if arr.len() >= 4 && arr[2].as_str() == Some("ticker") {
                let pair_raw = arr[3].as_str().unwrap_or("");
                let symbol = normalize_kraken_symbol(pair_raw);
                let data = &arr[1];

                // b = best bid, a = best ask
                let bid = data["b"][0]
                    .as_str()
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or(Decimal::ZERO);
                let ask = data["a"][0]
                    .as_str()
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or(Decimal::ZERO);
                let last = data["c"][0]
                    .as_str()
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or(Decimal::ZERO);

                if bid > Decimal::ZERO && ask > Decimal::ZERO {
                    let ticker = Ticker {
                        exchange: ExchangeId::Kraken,
                        symbol: symbol.clone(),
                        bid,
                        ask,
                        last,
                        volume_24h: Decimal::ZERO,
                        timestamp: Utc::now(),
                    };

                    price_cache.insert((ExchangeId::Kraken, symbol.clone()), ticker.clone());
                    let _ = tx.send(MarketUpdate {
                        exchange: ExchangeId::Kraken,
                        symbol,
                        ticker: Some(ticker),
                        order_book: None,
                    });
                }
            }
        }

        Ok(())
    }

    // ─── DEX REST Polling ───────────────────────────────────────────────────

    async fn poll_dex_pool(
        exchange: &ExchangeId,
        symbol: &str,
        pool_cache: &PoolCache,
        rpc_url: &str,
    ) -> Result<()> {
        match exchange {
            ExchangeId::UniswapV3 => {
                if let Some(info) = get_v3_pool_info(symbol) {
                    let pool = Self::fetch_uniswap_v3_pool(rpc_url, exchange, symbol, &info).await?;
                    pool_cache.insert((exchange.clone(), symbol.to_string()), pool);
                }
            }
            ExchangeId::SushiSwap | ExchangeId::PancakeSwap => {
                if let Some(info) = get_v2_pool_info(exchange, symbol) {
                    let pool = Self::fetch_v2_pool(rpc_url, exchange, symbol, &info).await?;
                    pool_cache.insert((exchange.clone(), symbol.to_string()), pool);
                }
            }
            _ => {
                debug!("DEX polling not implemented for {:?}/{}", exchange, symbol);
            }
        }
        Ok(())
    }

    /// Fetch Uniswap V3 pool state via direct JSON-RPC.
    /// Calls slot0() for sqrtPriceX96 and liquidity() for the in-range liquidity,
    /// then derives virtual reserves using the x*y=k approximation.
    async fn fetch_uniswap_v3_pool(
        rpc_url: &str,
        exchange: &ExchangeId,
        symbol: &str,
        info: &DexPoolInfo,
    ) -> Result<PoolState> {
        // slot0() → (sqrtPriceX96, tick, ...)
        let slot0_hex = Self::eth_call(rpc_url, info.address, "0x3850c7bd").await?;
        // liquidity() → uint128
        let liq_hex = Self::eth_call(rpc_url, info.address, "0x1a686502").await?;

        let sqrt_px96 = abi_word_to_f64(&slot0_hex, 0)?;
        let liquidity = abi_word_to_u128(&liq_hex, 0)? as f64;

        if sqrt_px96 == 0.0 || liquidity == 0.0 {
            anyhow::bail!("UniswapV3 {}/{}: zero price or liquidity", exchange, symbol);
        }

        // Virtual reserves: at price P = (sqrtPriceX96/2^96)^2 (raw token units)
        //   reserve0_raw = L / sqrt_price_raw = L * 2^96 / sqrtPriceX96
        //   reserve1_raw = L * sqrt_price_raw = L * sqrtPriceX96 / 2^96
        let pow96 = 2f64.powi(96);
        let virtual0_raw = liquidity * pow96 / sqrt_px96;
        let virtual1_raw = liquidity * sqrt_px96 / pow96;

        // Decimal-adjust
        let pow_t0 = 10f64.powi(info.token0_decimals as i32);
        let pow_t1 = 10f64.powi(info.token1_decimals as i32);
        let (r0, r1) = if info.invert {
            (virtual1_raw / pow_t1, virtual0_raw / pow_t0)
        } else {
            (virtual0_raw / pow_t0, virtual1_raw / pow_t1)
        };

        let (base, quote) = split_symbol(symbol);
        Ok(PoolState {
            exchange: exchange.clone(),
            pool_address: info.address.to_string(),
            token0: base.to_string(),
            token1: quote.to_string(),
            reserve0: Decimal::from_str(&format!("{:.6}", r0)).unwrap_or(Decimal::ZERO),
            reserve1: Decimal::from_str(&format!("{:.6}", r1)).unwrap_or(Decimal::ZERO),
            fee_tier: info.fee_tier,
            timestamp: Utc::now(),
        })
    }

    /// Fetch Uniswap V2-style pool (SushiSwap / PancakeSwap) via getReserves().
    async fn fetch_v2_pool(
        rpc_url: &str,
        exchange: &ExchangeId,
        symbol: &str,
        info: &DexPoolInfo,
    ) -> Result<PoolState> {
        // getReserves() → (uint112 reserve0, uint112 reserve1, uint32 timestamp)
        let hex = Self::eth_call(rpc_url, info.address, "0x0902f1ac").await?;

        let r0_raw = abi_word_to_u128(&hex, 0)? as f64;
        let r1_raw = abi_word_to_u128(&hex, 1)? as f64;

        if r0_raw == 0.0 || r1_raw == 0.0 {
            anyhow::bail!("V2 {}/{}: zero reserves", exchange, symbol);
        }

        let pow_t0 = 10f64.powi(info.token0_decimals as i32);
        let pow_t1 = 10f64.powi(info.token1_decimals as i32);
        let (r0, r1) = if info.invert {
            (r1_raw / pow_t1, r0_raw / pow_t0)
        } else {
            (r0_raw / pow_t0, r1_raw / pow_t1)
        };

        let (base, quote) = split_symbol(symbol);
        Ok(PoolState {
            exchange: exchange.clone(),
            pool_address: info.address.to_string(),
            token0: base.to_string(),
            token1: quote.to_string(),
            reserve0: Decimal::from_str(&format!("{:.6}", r0)).unwrap_or(Decimal::ZERO),
            reserve1: Decimal::from_str(&format!("{:.6}", r1)).unwrap_or(Decimal::ZERO),
            fee_tier: info.fee_tier,
            timestamp: Utc::now(),
        })
    }

    /// Send an eth_call JSON-RPC request and return the hex result string (without "0x").
    async fn eth_call(rpc_url: &str, contract: &str, data: &str) -> Result<String> {
        let client = HttpClient::builder()
            .timeout(Duration::from_secs(5))
            .build()?;

        let body = json!({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract, "data": data}, "latest"],
            "id": 1
        });

        let resp: Value = client
            .post(rpc_url)
            .json(&body)
            .send()
            .await
            .context("RPC request failed")?
            .json()
            .await
            .context("RPC response parse failed")?;

        if let Some(err) = resp.get("error") {
            anyhow::bail!("RPC error: {}", err);
        }

        let result = resp["result"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing 'result' in RPC response"))?;

        // Strip "0x" prefix
        Ok(result.trim_start_matches("0x").to_string())
    }

    /// Persist a ticker to Redis for cross-process sharing
    pub async fn cache_to_redis(&self, ticker: &Ticker) -> Result<()> {
        let Some(client) = &self.redis_client else {
            return Ok(());
        };
        let mut conn = client.get_async_connection().await?;
        let key = format!(
            "arb:ticker:{}:{}",
            ticker.exchange.as_str(),
            ticker.symbol.replace('/', "_")
        );
        let value = serde_json::to_string(ticker)?;
        let ttl_secs: u64 = self.config.redis.cache_ttl_ms / 1000 + 1;
        conn.set_ex::<_, _, ()>(&key, value, ttl_secs).await?;
        Ok(())
    }
}

// ─── DEX Pool Info ───────────────────────────────────────────────────────────

/// Static pool metadata for a DEX pair.
struct DexPoolInfo {
    /// On-chain pool/pair contract address.
    address: &'static str,
    /// Pool fee tier as a fraction (e.g. 0.003 = 0.3%).
    fee_tier: Decimal,
    /// Decimals of the on-chain token0.
    token0_decimals: u8,
    /// Decimals of the on-chain token1.
    token1_decimals: u8,
    /// True when the on-chain token ordering is inverted relative to the
    /// trading pair (token0 is the quote, token1 is the base).
    /// When true, reserve0/reserve1 are swapped before building PoolState.
    invert: bool,
}

fn get_v3_pool_info(symbol: &str) -> Option<DexPoolInfo> {
    // Uniswap V3 Ethereum mainnet pools.
    // token0 < token1 by address (lexicographic).
    match symbol {
        "ETH/USDT" => Some(DexPoolInfo {
            // WETH (0xC02…) < USDT (0xdAC…) → token0=WETH, token1=USDT
            address: "0x4e68Ccd3E89f51C3074ca5072bbAC773960dFa36",
            fee_tier: Decimal::from_str("0.003").unwrap(),
            token0_decimals: 18, // WETH
            token1_decimals: 6,  // USDT
            invert: false,
        }),
        "BTC/USDT" => Some(DexPoolInfo {
            // WBTC (0x2260…) < USDT (0xdAC…) → token0=WBTC, token1=USDT
            address: "0x9Db9e0e53058C89e5B94e29621a205198648425B",
            fee_tier: Decimal::from_str("0.003").unwrap(),
            token0_decimals: 8, // WBTC
            token1_decimals: 6, // USDT
            invert: false,
        }),
        // SOL is not an ERC-20; ARB is on Arbitrum L2 — skip mainnet.
        _ => None,
    }
}

fn get_v2_pool_info(exchange: &ExchangeId, symbol: &str) -> Option<DexPoolInfo> {
    match (exchange, symbol) {
        // ── SushiSwap (Ethereum mainnet, Uniswap V2 fork) ──────────────────
        (ExchangeId::SushiSwap, "ETH/USDT") => Some(DexPoolInfo {
            // WETH (0xC02…) < USDT (0xdAC…) → token0=WETH, token1=USDT
            address: "0x06da0fd433C1A5d7a4faa01111c044910A184553",
            fee_tier: Decimal::from_str("0.003").unwrap(),
            token0_decimals: 18,
            token1_decimals: 6,
            invert: false,
        }),
        (ExchangeId::SushiSwap, "BTC/USDT") => Some(DexPoolInfo {
            // WBTC (0x2260…) < USDT (0xdAC…) → token0=WBTC, token1=USDT
            address: "0xCEfF51756c56CeFFCA006cD410B03FFC46dd3a58",
            fee_tier: Decimal::from_str("0.003").unwrap(),
            token0_decimals: 8,
            token1_decimals: 6,
            invert: false,
        }),
        // ── PancakeSwap V2 (BSC) ────────────────────────────────────────────
        (ExchangeId::PancakeSwap, "BNB/USDT") => Some(DexPoolInfo {
            // On BSC: USDT (0x55d3…) < WBNB (0xbb4C…) → token0=USDT, token1=WBNB
            // BSC USDT has 18 decimals. Invert so PoolState.price() = USDT/BNB.
            address: "0x16b9a82891338f9bA80E2D6970fdda79D1eb0daE",
            fee_tier: Decimal::from_str("0.0025").unwrap(),
            token0_decimals: 18, // on-chain token0 = BSC USDT
            token1_decimals: 18, // on-chain token1 = WBNB
            invert: true,
        }),
        (ExchangeId::PancakeSwap, "BTC/USDT") => Some(DexPoolInfo {
            // BTCB (0x7130…) > USDT (0x55d3…) → token0=USDT, token1=BTCB
            // Invert so PoolState.price() = USDT/BTC.
            address: "0x3F803EC2b816Ea7F06EC76aA2B6f2532F9892d62",
            fee_tier: Decimal::from_str("0.0025").unwrap(),
            token0_decimals: 18, // on-chain token0 = BSC USDT
            token1_decimals: 18, // on-chain token1 = BTCB
            invert: true,
        }),
        (ExchangeId::PancakeSwap, "ETH/USDT") => Some(DexPoolInfo {
            // WETH (0x2170…) > USDT (0x55d3…) → token0=USDT, token1=WETH
            // Invert so PoolState.price() = USDT/ETH.
            address: "0x531FEbfeb37a3D0803AB28d26cF7ec7bAC45C8F7",
            fee_tier: Decimal::from_str("0.0025").unwrap(),
            token0_decimals: 18, // on-chain token0 = BSC USDT
            token1_decimals: 18, // on-chain token1 = WETH
            invert: true,
        }),
        _ => None,
    }
}

// ─── ABI Decode Helpers ──────────────────────────────────────────────────────

/// Parse a 32-byte ABI word (64 hex chars, no "0x") at `word_idx` as u128.
/// Works for uint112 and uint128; the value is right-justified in the word.
fn abi_word_to_u128(hex: &str, word_idx: usize) -> Result<u128> {
    let start = word_idx * 64;
    if hex.len() < start + 64 {
        anyhow::bail!("ABI response too short (have {}, need {})", hex.len(), start + 64);
    }
    // Lower 32 hex chars = 16 bytes = 128 bits (value is right-padded to 256 bits)
    let lower = &hex[start + 32..start + 64];
    u128::from_str_radix(lower, 16)
        .map_err(|e| anyhow::anyhow!("abi_word_to_u128 '{}': {}", lower, e))
}

/// Parse a 32-byte ABI word as f64. Used for uint160 (sqrtPriceX96) where
/// the value can exceed u128 but f64 precision is sufficient for price math.
fn abi_word_to_f64(hex: &str, word_idx: usize) -> Result<f64> {
    let start = word_idx * 64;
    if hex.len() < start + 64 {
        anyhow::bail!("ABI response too short");
    }
    let word = &hex[start..start + 64];
    // Split into four 64-bit chunks and accumulate.
    let a = u64::from_str_radix(&word[0..16], 16).unwrap_or(0) as f64;
    let b = u64::from_str_radix(&word[16..32], 16).unwrap_or(0) as f64;
    let c = u64::from_str_radix(&word[32..48], 16).unwrap_or(0) as f64;
    let d = u64::from_str_radix(&word[48..64], 16).unwrap_or(0) as f64;
    Ok(a * 2f64.powi(192) + b * 2f64.powi(128) + c * 2f64.powi(64) + d)
}

/// Split "ETH/USDT" into ("ETH", "USDT").
fn split_symbol(symbol: &str) -> (&str, &str) {
    if let Some(idx) = symbol.find('/') {
        (&symbol[..idx], &symbol[idx + 1..])
    } else {
        (symbol, "USDT")
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

fn parse_decimal(v: &Value) -> Result<Decimal> {
    let s = match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        _ => return Err(anyhow::anyhow!("Cannot parse decimal from {:?}", v)),
    };
    Decimal::from_str(&s).map_err(|e| anyhow::anyhow!("Decimal parse error '{}': {}", s, e))
}

fn parse_decimal_opt(v: &Value) -> Option<Decimal> {
    parse_decimal(v).ok()
}

fn parse_binance_levels(v: &Value) -> Result<Vec<OrderBookLevel>> {
    let arr = v
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Expected array for order book levels"))?;

    arr.iter()
        .map(|level| {
            let price = parse_decimal(&level[0])?;
            let qty = parse_decimal(&level[1])?;
            Ok(OrderBookLevel {
                price,
                quantity: qty,
            })
        })
        .collect()
}

fn normalize_binance_symbol(raw: &str) -> String {
    // ETHUSDT -> ETH/USDT, BTCUSDT -> BTC/USDT
    for base in &["BTC", "ETH", "BNB", "SOL", "ARB", "AVAX", "MATIC"] {
        for quote in &["USDT", "USDC", "BUSD", "BTC", "ETH"] {
            let candidate = format!("{}{}", base, quote);
            if raw.eq_ignore_ascii_case(&candidate) {
                return format!("{}/{}", base, quote);
            }
        }
    }
    // Generic fallback: assume last 4 chars are quote
    if raw.len() > 4 {
        let (base, quote) = raw.split_at(raw.len() - 4);
        return format!("{}/{}", base, quote);
    }
    raw.to_string()
}

fn normalize_bybit_symbol(raw: &str) -> String {
    normalize_binance_symbol(raw)
}

fn normalize_kraken_symbol(raw: &str) -> String {
    // XBT/USDT -> BTC/USDT
    raw.replace("XBT", "BTC")
        .replace("XXBT", "BTC")
        .replace("XETH", "ETH")
        .replace("ZUSD", "USD")
}
