use crate::config::AppConfig;
use crate::data::types::{ExchangeId, MarketSnapshot};
use crate::engine::pricing_engine::{PriceQuote, PricingEngine};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tracing::{debug, info};
use uuid::Uuid;

/// A detected arbitrage opportunity
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArbitrageOpportunity {
    pub id: String,
    /// Buy on this exchange (lower price)
    pub buy_exchange: ExchangeId,
    /// Sell on this exchange (higher price)
    pub sell_exchange: ExchangeId,
    pub symbol: String,
    /// The price at which we buy
    pub buy_price: Decimal,
    /// The price at which we sell
    pub sell_price: Decimal,
    /// Gross profit = sell_price - buy_price (per unit)
    pub gross_spread: Decimal,
    /// Gross spread as a percentage of buy price
    pub gross_spread_pct: Decimal,
    /// Total fees (trading + transfer + gas)
    pub total_fees: Decimal,
    /// Net profit in USD for the given trade size
    pub net_profit_usd: Decimal,
    /// Net profit as a percentage of capital deployed
    pub net_profit_pct: Decimal,
    /// Trade size in USD
    pub trade_size_usd: Decimal,
    /// Asset quantity to trade
    pub trade_quantity: Decimal,
    /// Estimated slippage on buy leg
    pub buy_slippage: Decimal,
    /// Estimated slippage on sell leg
    pub sell_slippage: Decimal,
    /// Available liquidity on buy side
    pub buy_liquidity_usd: Decimal,
    /// Available liquidity on sell side
    pub sell_liquidity_usd: Decimal,
    /// Confidence score 0.0-1.0
    pub confidence: Decimal,
    pub detected_at: DateTime<Utc>,
    /// Time this opportunity is expected to remain valid
    pub ttl_ms: u64,
}

impl ArbitrageOpportunity {
    pub fn is_expired(&self) -> bool {
        let elapsed = Utc::now()
            .signed_duration_since(self.detected_at)
            .num_milliseconds();
        elapsed > self.ttl_ms as i64
    }
}

/// Core arbitrage detection engine
pub struct ArbitrageDetector {
    config: Arc<AppConfig>,
    pricing_engine: PricingEngine,
}

impl ArbitrageDetector {
    pub fn new(config: Arc<AppConfig>) -> Self {
        let pricing_engine = PricingEngine::new(Arc::clone(&config));
        Self {
            config,
            pricing_engine,
        }
    }

    /// Scan a market snapshot and return all profitable arbitrage opportunities
    pub fn detect(&self, snapshot: &MarketSnapshot) -> Vec<ArbitrageOpportunity> {
        let trade_size = self.config.trading.max_trade_size_usd;
        let min_threshold = self.config.trading.min_profit_threshold;
        let max_slippage = self.config.trading.max_slippage;
        let min_liquidity = self.config.trading.min_liquidity_usd;

        let quotes = self.pricing_engine.get_quotes(snapshot, trade_size);

        if quotes.len() < 2 {
            debug!("Not enough quotes for {}", snapshot.symbol);
            return vec![];
        }

        let mut opportunities = Vec::new();

        // Compare all pairs of exchanges
        for i in 0..quotes.len() {
            for j in 0..quotes.len() {
                if i == j {
                    continue;
                }

                let buy_quote = &quotes[i];
                let sell_quote = &quotes[j];

                // We buy at ask on exchange i, sell at bid on exchange j
                let buy_price = buy_quote.effective_ask;
                let sell_price = sell_quote.effective_bid;

                if buy_price <= Decimal::ZERO || sell_price <= Decimal::ZERO {
                    continue;
                }

                // Liquidity check
                if buy_quote.ask_liquidity_usd < min_liquidity {
                    debug!(
                        "Insufficient buy liquidity on {}: {}",
                        buy_quote.exchange, buy_quote.ask_liquidity_usd
                    );
                    continue;
                }
                if sell_quote.bid_liquidity_usd < min_liquidity {
                    debug!(
                        "Insufficient sell liquidity on {}: {}",
                        sell_quote.exchange, sell_quote.bid_liquidity_usd
                    );
                    continue;
                }

                // Slippage check
                let buy_slippage =
                    PricingEngine::slippage_pct(buy_quote.ask, buy_price);
                let sell_slippage =
                    PricingEngine::slippage_pct(sell_quote.bid, sell_price);

                if buy_slippage > max_slippage {
                    debug!(
                        "Buy slippage too high on {}: {}%",
                        buy_quote.exchange,
                        buy_slippage * dec!(100)
                    );
                    continue;
                }
                if sell_slippage > max_slippage {
                    debug!(
                        "Sell slippage too high on {}: {}%",
                        sell_quote.exchange,
                        sell_slippage * dec!(100)
                    );
                    continue;
                }

                let gross_spread = sell_price - buy_price;
                if gross_spread <= Decimal::ZERO {
                    continue;
                }

                let gross_spread_pct = gross_spread / buy_price;

                // Calculate trade quantity from USD size
                let trade_quantity = if buy_price > Decimal::ZERO {
                    trade_size / buy_price
                } else {
                    continue;
                };

                // Fee calculation:
                // Buy leg: taker fee on buy exchange
                let buy_fee_usd = trade_size * buy_quote.taker_fee;
                // Sell leg: taker fee on sell exchange
                let sell_fee_usd = trade_size * sell_quote.taker_fee;
                // Transfer cost (withdrawal or gas)
                let transfer_fee_usd = buy_quote.transfer_cost_usd.max(sell_quote.transfer_cost_usd);

                let total_fees = buy_fee_usd + sell_fee_usd + transfer_fee_usd;

                // Net profit calculation
                let gross_profit_usd = gross_spread * trade_quantity;
                let net_profit_usd = gross_profit_usd - total_fees;
                let net_profit_pct = net_profit_usd / trade_size;

                if net_profit_pct < min_threshold {
                    debug!(
                        "Opportunity below threshold: {} -> {} {:.4}% (need {:.4}%)",
                        buy_quote.exchange,
                        sell_quote.exchange,
                        net_profit_pct * dec!(100),
                        min_threshold * dec!(100)
                    );
                    continue;
                }

                // Confidence scoring
                let confidence = self.calculate_confidence(
                    buy_quote,
                    sell_quote,
                    gross_spread_pct,
                    buy_slippage,
                    sell_slippage,
                );

                let opportunity = ArbitrageOpportunity {
                    id: Uuid::new_v4().to_string(),
                    buy_exchange: buy_quote.exchange.clone(),
                    sell_exchange: sell_quote.exchange.clone(),
                    symbol: snapshot.symbol.clone(),
                    buy_price,
                    sell_price,
                    gross_spread,
                    gross_spread_pct,
                    total_fees,
                    net_profit_usd,
                    net_profit_pct,
                    trade_size_usd: trade_size,
                    trade_quantity,
                    buy_slippage,
                    sell_slippage,
                    buy_liquidity_usd: buy_quote.ask_liquidity_usd,
                    sell_liquidity_usd: sell_quote.bid_liquidity_usd,
                    confidence,
                    detected_at: Utc::now(),
                    ttl_ms: 500, // opportunities are valid for 500ms
                };

                info!(
                    "Opportunity detected: Buy {} on {} @ {:.4}, Sell on {} @ {:.4}, Net profit: {:.4}% (${:.2})",
                    opportunity.symbol,
                    opportunity.buy_exchange,
                    opportunity.buy_price,
                    opportunity.sell_exchange,
                    opportunity.sell_price,
                    opportunity.net_profit_pct * dec!(100),
                    opportunity.net_profit_usd,
                );

                opportunities.push(opportunity);
            }
        }

        // Sort by net profit descending
        opportunities.sort_by(|a, b| {
            b.net_profit_pct
                .partial_cmp(&a.net_profit_pct)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        opportunities
    }

    /// Calculate a confidence score (0.0 to 1.0) for an opportunity
    fn calculate_confidence(
        &self,
        buy_quote: &PriceQuote,
        sell_quote: &PriceQuote,
        gross_spread_pct: Decimal,
        buy_slippage: Decimal,
        sell_slippage: Decimal,
    ) -> Decimal {
        let mut score = dec!(1.0);

        // Penalize high slippage
        let total_slippage = buy_slippage + sell_slippage;
        if total_slippage > dec!(0.001) {
            score -= (total_slippage / dec!(0.003)).min(dec!(0.5));
        }

        // Penalize low liquidity relative to trade size
        let min_liq = buy_quote
            .ask_liquidity_usd
            .min(sell_quote.bid_liquidity_usd);
        let liq_ratio = min_liq / self.config.trading.max_trade_size_usd;
        if liq_ratio < dec!(10) {
            score -= (dec!(1) - liq_ratio / dec!(10)) * dec!(0.3);
        }

        // Penalize very high spreads (could be stale data)
        if gross_spread_pct > dec!(0.05) {
            score -= dec!(0.2);
        }

        // Penalize DEX-to-DEX (higher execution complexity)
        if buy_quote.exchange.is_dex() && sell_quote.exchange.is_dex() {
            score -= dec!(0.1);
        }

        score.max(Decimal::ZERO).min(Decimal::ONE)
    }

    /// Quick-scan using only top-of-book prices (no order book walk)
    /// Used for fast initial filtering before full analysis
    pub fn quick_scan(&self, snapshot: &MarketSnapshot) -> bool {
        let min_gross = self.config.trading.min_profit_threshold + dec!(0.002); // add buffer for fees

        let tickers: Vec<_> = snapshot.tickers.values().collect();
        if tickers.len() < 2 {
            return false;
        }

        let max_bid = tickers.iter().map(|t| t.bid).max().unwrap_or(Decimal::ZERO);
        let min_ask = tickers.iter().map(|t| t.ask).min().unwrap_or(Decimal::MAX);

        if min_ask <= Decimal::ZERO {
            return false;
        }

        let raw_spread = (max_bid - min_ask) / min_ask;
        raw_spread > min_gross
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::data::types::Ticker;
    use chrono::Utc;
    use rust_decimal_macros::dec;
    use std::collections::HashMap;

    fn make_ticker(exchange: ExchangeId, bid: Decimal, ask: Decimal) -> Ticker {
        Ticker {
            exchange: exchange.clone(),
            symbol: "ETH/USDT".into(),
            bid,
            ask,
            last: (bid + ask) / dec!(2),
            volume_24h: dec!(1000000),
            timestamp: Utc::now(),
        }
    }

    #[test]
    fn test_quick_scan_detects_spread() {
        let config = Arc::new(create_test_config());
        let detector = ArbitrageDetector::new(config);

        let mut tickers = HashMap::new();
        tickers.insert(
            ExchangeId::Binance,
            make_ticker(ExchangeId::Binance, dec!(2000), dec!(2001)),
        );
        tickers.insert(
            ExchangeId::Okx,
            make_ticker(ExchangeId::Okx, dec!(2025), dec!(2026)),
        );

        let snapshot = MarketSnapshot {
            symbol: "ETH/USDT".into(),
            tickers,
            order_books: HashMap::new(),
            pool_states: HashMap::new(),
            captured_at: Utc::now(),
        };

        assert!(detector.quick_scan(&snapshot));
    }

    #[test]
    fn test_no_opportunity_when_spread_too_small() {
        let config = Arc::new(create_test_config());
        let detector = ArbitrageDetector::new(config);

        let mut tickers = HashMap::new();
        tickers.insert(
            ExchangeId::Binance,
            make_ticker(ExchangeId::Binance, dec!(2000), dec!(2001)),
        );
        tickers.insert(
            ExchangeId::Okx,
            make_ticker(ExchangeId::Okx, dec!(2001), dec!(2002)), // barely higher
        );

        let snapshot = MarketSnapshot {
            symbol: "ETH/USDT".into(),
            tickers,
            order_books: HashMap::new(),
            pool_states: HashMap::new(),
            captured_at: Utc::now(),
        };

        // No order books, small spread — should return no opportunities after fee deduction
        let opps = detector.detect(&snapshot);
        for opp in &opps {
            assert!(opp.net_profit_pct > dec!(0.006));
        }
    }

    fn create_test_config() -> AppConfig {
        use crate::config::*;
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
                binance: make_exchange_config(dec!(0.001), dec!(0.001), dec!(0.005)),
                okx: make_exchange_config(dec!(0.0008), dec!(0.001), dec!(0.003)),
                bybit: make_exchange_config(dec!(0.001), dec!(0.001), dec!(0.004)),
                kraken: make_exchange_config(dec!(0.0016), dec!(0.0026), dec!(0.0035)),
            },
            dex: DexConfig {
                uniswap_v3: DexExchangeConfig {
                    enabled: false,
                    rpc_url: "".into(),
                    router_address: "".into(),
                    factory_address: None,
                    gas_limit: None,
                },
                sushiswap: DexExchangeConfig {
                    enabled: false,
                    rpc_url: "".into(),
                    router_address: "".into(),
                    factory_address: None,
                    gas_limit: None,
                },
                pancakeswap: DexExchangeConfig {
                    enabled: false,
                    rpc_url: "".into(),
                    router_address: "".into(),
                    factory_address: None,
                    gas_limit: None,
                },
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
                level: "info".into(),
                file_path: "logs/arb-bot.log".into(),
                json_format: true,
            },
        }
    }

    fn make_exchange_config(
        maker_fee: Decimal,
        taker_fee: Decimal,
        withdrawal_fee_eth: Decimal,
    ) -> crate::config::ExchangeConfig {
        crate::config::ExchangeConfig {
            enabled: true,
            ws_url: "wss://example.com".into(),
            rest_url: "https://example.com".into(),
            api_key: "".into(),
            api_secret: "".into(),
            passphrase: None,
            maker_fee,
            taker_fee,
            withdrawal_fee_eth,
        }
    }
}
