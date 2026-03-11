use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Identifies which exchange provided the data
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ExchangeId {
    Binance,
    Okx,
    Bybit,
    Kraken,
    UniswapV3,
    SushiSwap,
    PancakeSwap,
    Balancer,
}

impl ExchangeId {
    pub fn as_str(&self) -> &'static str {
        match self {
            ExchangeId::Binance => "binance",
            ExchangeId::Okx => "okx",
            ExchangeId::Bybit => "bybit",
            ExchangeId::Kraken => "kraken",
            ExchangeId::UniswapV3 => "uniswap_v3",
            ExchangeId::SushiSwap => "sushiswap",
            ExchangeId::PancakeSwap => "pancakeswap",
            ExchangeId::Balancer => "balancer",
        }
    }

    pub fn is_dex(&self) -> bool {
        matches!(
            self,
            ExchangeId::UniswapV3
                | ExchangeId::SushiSwap
                | ExchangeId::PancakeSwap
                | ExchangeId::Balancer
        )
    }
}

impl std::fmt::Display for ExchangeId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// A single price level in an order book
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBookLevel {
    pub price: Decimal,
    pub quantity: Decimal,
}

/// Simplified order book snapshot (top N levels)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBook {
    pub exchange: ExchangeId,
    pub symbol: String,
    pub bids: Vec<OrderBookLevel>, // sorted descending (best bid first)
    pub asks: Vec<OrderBookLevel>, // sorted ascending (best ask first)
    pub timestamp: DateTime<Utc>,
}

impl OrderBook {
    pub fn best_bid(&self) -> Option<&OrderBookLevel> {
        self.bids.first()
    }

    pub fn best_ask(&self) -> Option<&OrderBookLevel> {
        self.asks.first()
    }

    /// Calculate the effective execution price for a given USD notional on the bid side.
    /// Walks the order book to account for slippage.
    pub fn effective_sell_price(&self, usd_notional: Decimal) -> Option<Decimal> {
        self.effective_price(&self.bids, usd_notional)
    }

    /// Calculate the effective execution price for a given USD notional on the ask side.
    pub fn effective_buy_price(&self, usd_notional: Decimal) -> Option<Decimal> {
        self.effective_price(&self.asks, usd_notional)
    }

    fn effective_price(&self, levels: &[OrderBookLevel], usd_notional: Decimal) -> Option<Decimal> {
        if levels.is_empty() {
            return None;
        }

        let mut remaining_usd = usd_notional;
        let mut total_cost = Decimal::ZERO;
        let mut total_qty = Decimal::ZERO;

        for level in levels {
            if remaining_usd <= Decimal::ZERO {
                break;
            }
            let level_value = level.price * level.quantity;
            let fill_value = remaining_usd.min(level_value);
            let fill_qty = fill_value / level.price;

            total_cost += fill_qty * level.price;
            total_qty += fill_qty;
            remaining_usd -= fill_value;
        }

        if total_qty > Decimal::ZERO {
            Some(total_cost / total_qty)
        } else {
            None
        }
    }

    /// Total available liquidity (USD) on the bid side
    pub fn bid_liquidity_usd(&self) -> Decimal {
        self.bids
            .iter()
            .map(|l| l.price * l.quantity)
            .sum()
    }

    /// Total available liquidity (USD) on the ask side
    pub fn ask_liquidity_usd(&self) -> Decimal {
        self.asks
            .iter()
            .map(|l| l.price * l.quantity)
            .sum()
    }
}

/// Normalized ticker / price snapshot from any exchange
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Ticker {
    pub exchange: ExchangeId,
    pub symbol: String,
    pub bid: Decimal,
    pub ask: Decimal,
    pub last: Decimal,
    pub volume_24h: Decimal,
    pub timestamp: DateTime<Utc>,
}

impl Ticker {
    pub fn mid_price(&self) -> Decimal {
        (self.bid + self.ask) / Decimal::from(2)
    }

    pub fn spread(&self) -> Decimal {
        self.ask - self.bid
    }

    pub fn spread_pct(&self) -> Decimal {
        if self.bid > Decimal::ZERO {
            self.spread() / self.bid
        } else {
            Decimal::ZERO
        }
    }
}

/// DEX liquidity pool state
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PoolState {
    pub exchange: ExchangeId,
    pub pool_address: String,
    pub token0: String,
    pub token1: String,
    pub reserve0: Decimal,
    pub reserve1: Decimal,
    pub fee_tier: Decimal, // e.g. 0.003 for 0.3%
    pub timestamp: DateTime<Utc>,
}

impl PoolState {
    /// Constant product AMM price: reserve1/reserve0
    pub fn price(&self) -> Decimal {
        if self.reserve0 > Decimal::ZERO {
            self.reserve1 / self.reserve0
        } else {
            Decimal::ZERO
        }
    }

    /// Simulate a swap and return the output amount accounting for AMM slippage + fees
    /// amount_in: how much token0 we're selling
    pub fn simulate_swap_token0_to_token1(&self, amount_in: Decimal) -> Decimal {
        let fee_multiplier = Decimal::ONE - self.fee_tier;
        let amount_in_with_fee = amount_in * fee_multiplier;
        // x * y = k  =>  dy = y * dx / (x + dx)
        (self.reserve1 * amount_in_with_fee) / (self.reserve0 + amount_in_with_fee)
    }

    /// Simulate a swap: how much token0 needed to receive amount_out token1
    pub fn simulate_swap_token1_to_token0_out(&self, amount_out: Decimal) -> Decimal {
        // dy = y * dx / (x + dx)  =>  dx = x * dy / (y - dy) / fee_multiplier
        if self.reserve1 <= amount_out {
            return Decimal::MAX;
        }
        let fee_multiplier = Decimal::ONE - self.fee_tier;
        (self.reserve0 * amount_out) / ((self.reserve1 - amount_out) * fee_multiplier)
    }

    /// Total liquidity in USD terms (reserve1 value)
    pub fn liquidity_usd(&self) -> Decimal {
        self.reserve1 * Decimal::from(2)
    }
}

/// A market snapshot combining all available data for a symbol
#[derive(Debug, Clone)]
pub struct MarketSnapshot {
    pub symbol: String,
    pub tickers: std::collections::HashMap<ExchangeId, Ticker>,
    pub order_books: std::collections::HashMap<ExchangeId, OrderBook>,
    pub pool_states: std::collections::HashMap<ExchangeId, PoolState>,
    pub captured_at: DateTime<Utc>,
}
