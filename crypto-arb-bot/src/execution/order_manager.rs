use crate::engine::arbitrage_detector::ArbitrageOpportunity;
use crate::execution::exchange_connector::{
    ExchangeConnector, OrderRequest, OrderResponse, OrderSide, OrderStatus, OrderType,
};
use crate::monitoring::metrics::Metrics;
use crate::risk::risk_manager::RiskManager;
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use dashmap::DashMap;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;
use tracing::{error, info, warn};
use uuid::Uuid;

/// State of an arbitrage trade
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum TradeState {
    Pending,
    BuySubmitted,
    BuyFilled,
    SellSubmitted,
    Completed,
    Failed(String),
    TimedOut,
}

/// A full arbitrage trade record
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArbTrade {
    pub id: String,
    pub opportunity_id: String,
    pub symbol: String,
    pub buy_exchange: String,
    pub sell_exchange: String,
    pub state: TradeState,
    pub buy_order: Option<OrderResponse>,
    pub sell_order: Option<OrderResponse>,
    pub planned_quantity: Decimal,
    pub planned_buy_price: Decimal,
    pub planned_sell_price: Decimal,
    pub actual_profit_usd: Option<Decimal>,
    pub fees_paid_usd: Decimal,
    pub created_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
    pub is_paper: bool,
}

impl ArbTrade {
    pub fn realized_pnl(&self) -> Option<Decimal> {
        match (&self.buy_order, &self.sell_order) {
            (Some(buy), Some(sell)) => {
                let buy_cost = buy.average_fill_price * buy.filled_quantity + buy.fee_paid;
                let sell_proceeds = sell.average_fill_price * sell.filled_quantity - sell.fee_paid;
                Some(sell_proceeds - buy_cost)
            }
            _ => None,
        }
    }
}

/// Manages order submission and tracking for arbitrage trades
pub struct OrderManager {
    connectors: Arc<DashMap<String, Arc<dyn ExchangeConnector>>>,
    active_trades: Arc<DashMap<String, ArbTrade>>,
    risk_manager: Arc<RiskManager>,
    metrics: Arc<Metrics>,
    order_timeout: Duration,
}

impl OrderManager {
    pub fn new(
        connectors: Arc<DashMap<String, Arc<dyn ExchangeConnector>>>,
        risk_manager: Arc<RiskManager>,
        metrics: Arc<Metrics>,
        order_timeout_secs: u64,
    ) -> Self {
        Self {
            connectors,
            active_trades: Arc::new(DashMap::new()),
            risk_manager,
            metrics,
            order_timeout: Duration::from_secs(order_timeout_secs),
        }
    }

    /// Execute a detected arbitrage opportunity
    pub async fn execute_opportunity(&self, opp: &ArbitrageOpportunity) -> Result<ArbTrade> {
        // Risk check before execution
        self.risk_manager
            .check_opportunity(opp)
            .context("Risk check failed")?;

        let trade_id = Uuid::new_v4().to_string();
        let buy_conn_key = opp.buy_exchange.as_str().to_string();
        let sell_conn_key = opp.sell_exchange.as_str().to_string();

        // Clone Arc out of DashMap Ref immediately to release the read lock
        let buy_connector: Arc<dyn ExchangeConnector> = self
            .connectors
            .get(&buy_conn_key)
            .map(|r| Arc::clone(&*r))
            .ok_or_else(|| anyhow::anyhow!("No connector for buy exchange: {}", buy_conn_key))?;

        let sell_connector: Arc<dyn ExchangeConnector> = self
            .connectors
            .get(&sell_conn_key)
            .map(|r| Arc::clone(&*r))
            .ok_or_else(|| anyhow::anyhow!("No connector for sell exchange: {}", sell_conn_key))?;

        let mut trade = ArbTrade {
            id: trade_id.clone(),
            opportunity_id: opp.id.clone(),
            symbol: opp.symbol.clone(),
            buy_exchange: buy_conn_key.clone(),
            sell_exchange: sell_conn_key.clone(),
            state: TradeState::Pending,
            buy_order: None,
            sell_order: None,
            planned_quantity: opp.trade_quantity,
            planned_buy_price: opp.buy_price,
            planned_sell_price: opp.sell_price,
            actual_profit_usd: None,
            fees_paid_usd: Decimal::ZERO,
            created_at: Utc::now(),
            completed_at: None,
            is_paper: true, // Will be set by connector
        };

        self.active_trades.insert(trade_id.clone(), trade.clone());

        info!(
            "Executing arb trade {}: Buy {} {} on {}, Sell on {}",
            trade_id, opp.trade_quantity, opp.symbol, opp.buy_exchange, opp.sell_exchange
        );

        // Submit both legs simultaneously
        let buy_req = OrderRequest {
            client_order_id: format!("{}-buy", trade_id),
            symbol: opp.symbol.clone(),
            side: OrderSide::Buy,
            order_type: OrderType::Market,
            quantity: opp.trade_quantity,
            notional_usd: Some(opp.trade_size_usd),
        };

        let sell_req = OrderRequest {
            client_order_id: format!("{}-sell", trade_id),
            symbol: opp.symbol.clone(),
            side: OrderSide::Sell,
            order_type: OrderType::Market,
            quantity: opp.trade_quantity,
            notional_usd: Some(opp.trade_size_usd),
        };

        // Submit both legs concurrently
        let (buy_result, sell_result) = tokio::join!(
            buy_connector.submit_order(&buy_req),
            sell_connector.submit_order(&sell_req),
        );

        match (buy_result, sell_result) {
            (Ok(buy_resp), Ok(sell_resp)) => {
                info!(
                    "Trade {} - Buy order: {}, Sell order: {}",
                    trade_id, buy_resp.exchange_order_id, sell_resp.exchange_order_id
                );

                trade.state = TradeState::BuySubmitted;
                trade.buy_order = Some(buy_resp.clone());
                trade.sell_order = Some(sell_resp.clone());

                // Wait for fills with timeout
                let timed_out_fallback = ArbTrade { state: TradeState::TimedOut, ..trade.clone() };
                let filled_trade = self
                    .wait_for_fills(trade, buy_connector.as_ref(), sell_connector.as_ref())
                    .await;

                let trade = filled_trade.unwrap_or_else(|e| {
                    error!("Trade {} fill timeout/error: {}", trade_id, e);
                    timed_out_fallback
                });

                self.on_trade_complete(&trade);
                self.active_trades.insert(trade_id, trade.clone());
                Ok(trade)
            }
            (Err(e), _) => {
                error!("Buy order failed for trade {}: {}", trade_id, e);
                trade.state = TradeState::Failed(format!("Buy order failed: {}", e));
                self.on_trade_failed(&trade);
                self.active_trades.insert(trade_id, trade.clone());
                Err(e.context("Buy order submission failed"))
            }
            (_, Err(e)) => {
                error!("Sell order failed for trade {}: {}", trade_id, e);
                // Critical: buy was submitted but sell failed — need to hedge
                warn!("UNHEDGED POSITION: buy submitted but sell failed for trade {}", trade_id);
                trade.state = TradeState::Failed(format!("Sell order failed: {}", e));
                self.on_trade_failed(&trade);
                self.active_trades.insert(trade_id, trade.clone());
                Err(e.context("Sell order submission failed"))
            }
        }
    }

    async fn wait_for_fills(
        &self,
        mut trade: ArbTrade,
        buy_connector: &dyn ExchangeConnector,
        sell_connector: &dyn ExchangeConnector,
    ) -> Result<ArbTrade> {
        let deadline = tokio::time::Instant::now() + self.order_timeout;
        let poll_interval = Duration::from_millis(100);

        let buy_order_id = trade
            .buy_order
            .as_ref()
            .map(|o| o.exchange_order_id.clone())
            .unwrap_or_default();
        let sell_order_id = trade
            .sell_order
            .as_ref()
            .map(|o| o.exchange_order_id.clone())
            .unwrap_or_default();

        let mut buy_filled = matches!(
            trade.buy_order.as_ref().map(|o| &o.status),
            Some(OrderStatus::Filled)
        );
        let mut sell_filled = matches!(
            trade.sell_order.as_ref().map(|o| &o.status),
            Some(OrderStatus::Filled)
        );

        while (!buy_filled || !sell_filled) && tokio::time::Instant::now() < deadline {
            tokio::time::sleep(poll_interval).await;

            if !buy_filled {
                if let Ok(status) = buy_connector
                    .get_order_status(&buy_order_id, &trade.symbol)
                    .await
                {
                    buy_filled = status.status == OrderStatus::Filled;
                    trade.buy_order = Some(status);
                }
            }

            if !sell_filled {
                if let Ok(status) = sell_connector
                    .get_order_status(&sell_order_id, &trade.symbol)
                    .await
                {
                    sell_filled = status.status == OrderStatus::Filled;
                    trade.sell_order = Some(status);
                }
            }
        }

        if buy_filled && sell_filled {
            trade.state = TradeState::Completed;
            trade.completed_at = Some(Utc::now());
            if let Some(pnl) = trade.realized_pnl() {
                trade.actual_profit_usd = Some(pnl);
            }
        } else {
            trade.state = TradeState::TimedOut;
        }

        Ok(trade)
    }

    fn on_trade_complete(&self, trade: &ArbTrade) {
        let pnl = trade.actual_profit_usd.unwrap_or(Decimal::ZERO);
        info!(
            "Trade {} completed. PnL: ${:.4}, State: {:?}",
            trade.id, pnl, trade.state
        );
        self.metrics.record_trade_completed(pnl);
        self.risk_manager.record_trade_result(pnl);
    }

    fn on_trade_failed(&self, trade: &ArbTrade) {
        warn!("Trade {} failed: {:?}", trade.id, trade.state);
        self.metrics.record_trade_failed();
        self.risk_manager.record_trade_result(Decimal::ZERO);
    }

    pub fn active_trade_count(&self) -> usize {
        self.active_trades
            .iter()
            .filter(|e| {
                matches!(
                    e.state,
                    TradeState::Pending
                        | TradeState::BuySubmitted
                        | TradeState::BuyFilled
                        | TradeState::SellSubmitted
                )
            })
            .count()
    }

    pub fn get_all_trades(&self) -> Vec<ArbTrade> {
        self.active_trades.iter().map(|e| e.clone()).collect()
    }
}
