use crate::config::AppConfig;
use crate::data::types::{ExchangeId, MarketSnapshot};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

/// A normalized price quote from any exchange (CEX or DEX)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriceQuote {
    pub exchange: ExchangeId,
    pub symbol: String,
    pub bid: Decimal,
    pub ask: Decimal,
    /// Effective bid after walking order book for trade size
    pub effective_bid: Decimal,
    /// Effective ask after walking order book for trade size
    pub effective_ask: Decimal,
    /// Available bid liquidity in USD
    pub bid_liquidity_usd: Decimal,
    /// Available ask liquidity in USD
    pub ask_liquidity_usd: Decimal,
    /// Taker fee for this exchange
    pub taker_fee: Decimal,
    /// Withdrawal/gas fee in USD equivalent
    pub transfer_cost_usd: Decimal,
}

/// Fee structure for an exchange
#[derive(Debug, Clone)]
pub struct FeeSchedule {
    pub maker_fee: Decimal,
    pub taker_fee: Decimal,
    pub withdrawal_fee_eth: Decimal,
    pub eth_price_usd: Decimal,
}

impl FeeSchedule {
    pub fn withdrawal_cost_usd(&self) -> Decimal {
        self.withdrawal_fee_eth * self.eth_price_usd
    }
}

/// The pricing engine normalizes data from multiple sources into comparable quotes
pub struct PricingEngine {
    config: Arc<AppConfig>,
}

impl PricingEngine {
    pub fn new(config: Arc<AppConfig>) -> Self {
        Self { config }
    }

    /// Build fee schedule for a CEX exchange
    pub fn fee_schedule_for(&self, exchange: &ExchangeId, eth_price: Decimal) -> FeeSchedule {
        let cfg = match exchange {
            ExchangeId::Binance => &self.config.exchanges.binance,
            ExchangeId::Okx => &self.config.exchanges.okx,
            ExchangeId::Bybit => &self.config.exchanges.bybit,
            ExchangeId::Kraken => &self.config.exchanges.kraken,
            _ => {
                return FeeSchedule {
                    maker_fee: dec!(0.003),
                    taker_fee: dec!(0.003),
                    withdrawal_fee_eth: dec!(0.005),
                    eth_price_usd: eth_price,
                }
            }
        };

        FeeSchedule {
            maker_fee: cfg.maker_fee,
            taker_fee: cfg.taker_fee,
            withdrawal_fee_eth: cfg.withdrawal_fee_eth,
            eth_price_usd: eth_price,
        }
    }

    /// Generate price quotes from a market snapshot for a given trade size
    pub fn get_quotes(
        &self,
        snapshot: &MarketSnapshot,
        trade_size_usd: Decimal,
    ) -> Vec<PriceQuote> {
        let mut quotes = Vec::new();

        // Estimate ETH price for fee calculations
        let eth_price = snapshot
            .tickers
            .values()
            .filter(|t| t.symbol.starts_with("ETH"))
            .map(|t| t.last)
            .next()
            .unwrap_or(dec!(2000));

        // CEX quotes from tickers + order books
        for (exchange, ticker) in &snapshot.tickers {
            let fee_schedule = self.fee_schedule_for(exchange, eth_price);

            let (effective_bid, effective_ask, bid_liq, ask_liq) =
                if let Some(ob) = snapshot.order_books.get(exchange) {
                    let eff_bid = ob
                        .effective_sell_price(trade_size_usd)
                        .unwrap_or(ticker.bid);
                    let eff_ask = ob
                        .effective_buy_price(trade_size_usd)
                        .unwrap_or(ticker.ask);
                    (
                        eff_bid,
                        eff_ask,
                        ob.bid_liquidity_usd(),
                        ob.ask_liquidity_usd(),
                    )
                } else {
                    // No order book, apply estimated slippage
                    let slippage = dec!(0.001); // 0.1% estimated slippage
                    (
                        ticker.bid * (Decimal::ONE - slippage),
                        ticker.ask * (Decimal::ONE + slippage),
                        trade_size_usd * dec!(5), // assume 5x trade size available
                        trade_size_usd * dec!(5),
                    )
                };

            quotes.push(PriceQuote {
                exchange: exchange.clone(),
                symbol: ticker.symbol.clone(),
                bid: ticker.bid,
                ask: ticker.ask,
                effective_bid,
                effective_ask,
                bid_liquidity_usd: bid_liq,
                ask_liquidity_usd: ask_liq,
                taker_fee: fee_schedule.taker_fee,
                transfer_cost_usd: fee_schedule.withdrawal_cost_usd(),
            });
        }

        // DEX quotes from pool states
        for (exchange, pool) in &snapshot.pool_states {
            if pool.liquidity_usd() < dec!(1000) {
                continue; // skip illiquid pools
            }

            let base_price = pool.price();
            let trade_qty = if base_price > Decimal::ZERO {
                trade_size_usd / base_price
            } else {
                continue;
            };

            let out_tokens = pool.simulate_swap_token0_to_token1(trade_qty);
            let effective_ask = if out_tokens > Decimal::ZERO {
                trade_size_usd / out_tokens
            } else {
                continue;
            };

            let in_tokens_for_sell = pool.simulate_swap_token1_to_token0_out(trade_qty);
            let effective_bid = if in_tokens_for_sell > Decimal::ZERO {
                trade_size_usd / in_tokens_for_sell
            } else {
                continue;
            };

            // DEX gas cost estimate: ~$15-50 depending on network
            let gas_cost_usd = dec!(25);

            quotes.push(PriceQuote {
                exchange: exchange.clone(),
                symbol: pool.token0.clone() + "/" + &pool.token1,
                bid: base_price,
                ask: base_price,
                effective_bid,
                effective_ask,
                bid_liquidity_usd: pool.liquidity_usd() / Decimal::from(2),
                ask_liquidity_usd: pool.liquidity_usd() / Decimal::from(2),
                taker_fee: pool.fee_tier,
                transfer_cost_usd: gas_cost_usd,
            });
        }

        quotes
    }

    /// Calculate slippage between quoted price and effective price
    pub fn slippage_pct(quoted: Decimal, effective: Decimal) -> Decimal {
        if quoted > Decimal::ZERO {
            (effective - quoted).abs() / quoted
        } else {
            Decimal::ZERO
        }
    }
}
