use crate::config::AppConfig;
use crate::engine::arbitrage_detector::ArbitrageOpportunity;
use anyhow::{bail, Result};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tracing::{error, info, warn};

/// Reason for a risk check failure
#[derive(Debug, thiserror::Error)]
pub enum RiskError {
    #[error("Circuit breaker triggered: daily loss ${0:.2} exceeds limit ${1:.2}")]
    CircuitBreakerTriggered(Decimal, Decimal),

    #[error("Maximum open positions ({0}) reached")]
    MaxPositionsReached(u32),

    #[error("Trade size ${0:.2} exceeds maximum ${1:.2}")]
    TradeSizeTooLarge(Decimal, Decimal),

    #[error("Insufficient liquidity: ${0:.2} available, ${1:.2} required")]
    InsufficientLiquidity(Decimal, Decimal),

    #[error("Slippage {0:.4}% exceeds maximum {1:.4}%")]
    SlippageTooHigh(Decimal, Decimal),

    #[error("Emergency shutdown is active")]
    EmergencyShutdown,

    #[error("Confidence score {0:.2} below threshold {1:.2}")]
    LowConfidence(Decimal, Decimal),

    #[error("Consecutive losses ({0}) exceeded limit ({1})")]
    TooManyConsecutiveLosses(u32, u32),
}

/// Internal state tracked by the risk manager
#[derive(Debug, Default)]
struct RiskState {
    daily_loss_usd: Decimal,
    daily_profit_usd: Decimal,
    open_positions: u32,
    consecutive_losses: u32,
    total_trades: u64,
    winning_trades: u64,
    day_start: Option<DateTime<Utc>>,
    trade_history: Vec<TradeRecord>,
}

#[derive(Debug, Clone)]
struct TradeRecord {
    timestamp: DateTime<Utc>,
    pnl: Decimal,
}

impl RiskState {
    fn reset_daily_if_needed(&mut self) {
        let now = Utc::now();
        let should_reset = match self.day_start {
            None => true,
            Some(start) => {
                (now - start) >= ChronoDuration::hours(24)
            }
        };

        if should_reset {
            info!("Resetting daily risk counters");
            self.daily_loss_usd = Decimal::ZERO;
            self.daily_profit_usd = Decimal::ZERO;
            self.day_start = Some(now);
        }
    }

    fn net_daily_pnl(&self) -> Decimal {
        self.daily_profit_usd - self.daily_loss_usd
    }

    fn win_rate(&self) -> Decimal {
        if self.total_trades == 0 {
            return Decimal::ONE;
        }
        Decimal::from(self.winning_trades) / Decimal::from(self.total_trades)
    }
}

/// Risk management system enforcing all trading safeguards
pub struct RiskManager {
    config: Arc<AppConfig>,
    state: Mutex<RiskState>,
    emergency_shutdown: AtomicBool,
    min_confidence: Decimal,
}

impl RiskManager {
    pub fn new(config: Arc<AppConfig>) -> Self {
        Self {
            config,
            state: Mutex::new(RiskState::default()),
            emergency_shutdown: AtomicBool::new(false),
            min_confidence: dec!(0.5),
        }
    }

    /// Perform all risk checks before allowing trade execution.
    /// Returns Ok(()) if the trade is safe to proceed.
    pub fn check_opportunity(&self, opp: &ArbitrageOpportunity) -> Result<()> {
        // Emergency shutdown takes priority
        if self.emergency_shutdown.load(Ordering::Relaxed) {
            return Err(RiskError::EmergencyShutdown.into());
        }

        let mut state = self.state.lock().unwrap();
        state.reset_daily_if_needed();

        // 1. Daily loss limit
        let max_daily_loss = self.config.trading.max_daily_loss_usd;
        if state.daily_loss_usd >= max_daily_loss {
            return Err(RiskError::CircuitBreakerTriggered(
                state.daily_loss_usd,
                max_daily_loss,
            )
            .into());
        }

        // 2. Max open positions
        let max_positions = self.config.risk.max_open_positions;
        if state.open_positions >= max_positions {
            return Err(RiskError::MaxPositionsReached(max_positions).into());
        }

        // 3. Trade size limit
        let max_size = self.config.trading.max_trade_size_usd;
        if opp.trade_size_usd > max_size {
            return Err(RiskError::TradeSizeTooLarge(opp.trade_size_usd, max_size).into());
        }

        // 4. Minimum liquidity
        let min_liq = self.config.trading.min_liquidity_usd;
        let available_liq = opp.buy_liquidity_usd.min(opp.sell_liquidity_usd);
        if available_liq < min_liq {
            return Err(RiskError::InsufficientLiquidity(available_liq, min_liq).into());
        }

        // 5. Slippage check
        let max_slippage = self.config.trading.max_slippage;
        let total_slippage = opp.buy_slippage + opp.sell_slippage;
        if total_slippage > max_slippage * dec!(2) {
            return Err(RiskError::SlippageTooHigh(
                total_slippage * dec!(100),
                max_slippage * dec!(200),
            )
            .into());
        }

        // 6. Confidence score
        if opp.confidence < self.min_confidence {
            return Err(
                RiskError::LowConfidence(opp.confidence, self.min_confidence).into(),
            );
        }

        // 7. Consecutive losses
        let max_consec = self.config.risk.max_consecutive_losses;
        if state.consecutive_losses >= max_consec {
            return Err(
                RiskError::TooManyConsecutiveLosses(state.consecutive_losses, max_consec).into(),
            );
        }

        // 8. Opportunity must not be expired
        if opp.is_expired() {
            bail!("Opportunity {} has expired", opp.id);
        }

        // All checks passed — increment open position counter
        state.open_positions += 1;

        Ok(())
    }

    /// Record the result of a completed trade
    pub fn record_trade_result(&self, pnl: Decimal) {
        let mut state = self.state.lock().unwrap();
        state.reset_daily_if_needed();

        state.open_positions = state.open_positions.saturating_sub(1);
        state.total_trades += 1;

        let record = TradeRecord {
            timestamp: Utc::now(),
            pnl,
        };
        state.trade_history.push(record);

        if pnl >= Decimal::ZERO {
            state.daily_profit_usd += pnl;
            state.winning_trades += 1;
            state.consecutive_losses = 0;
        } else {
            let loss = pnl.abs();
            state.daily_loss_usd += loss;
            state.consecutive_losses += 1;

            // Check circuit breaker
            let max_daily_loss = self.config.trading.max_daily_loss_usd;
            if state.daily_loss_usd >= max_daily_loss {
                error!(
                    "CIRCUIT BREAKER: Daily loss ${:.2} exceeded limit ${:.2}. Triggering emergency shutdown.",
                    state.daily_loss_usd, max_daily_loss
                );
                self.trigger_emergency_shutdown("Daily loss limit reached");
            }

            // Check portfolio-level circuit breaker
            let portfolio_loss_pct = self.config.risk.circuit_breaker_loss_pct;
            if loss / self.config.trading.max_trade_size_usd >= portfolio_loss_pct {
                warn!(
                    "Large single trade loss: ${:.2} ({:.1}% of max size)",
                    loss,
                    (loss / self.config.trading.max_trade_size_usd) * dec!(100)
                );
            }
        }

        info!(
            "Trade recorded. PnL: ${:.4}. Daily: profit=${:.2}, loss=${:.2}. Win rate: {:.1}%. Consecutive losses: {}",
            pnl,
            state.daily_profit_usd,
            state.daily_loss_usd,
            state.win_rate() * dec!(100),
            state.consecutive_losses,
        );
    }

    /// Trigger emergency shutdown — stops all future trading
    pub fn trigger_emergency_shutdown(&self, reason: &str) {
        error!("EMERGENCY SHUTDOWN TRIGGERED: {}", reason);
        self.emergency_shutdown.store(true, Ordering::Relaxed);
    }

    /// Manually reset emergency shutdown (requires human intervention)
    pub fn reset_emergency_shutdown(&self) {
        info!("Emergency shutdown reset by operator");
        self.emergency_shutdown.store(false, Ordering::Relaxed);
    }

    pub fn is_shutdown(&self) -> bool {
        self.emergency_shutdown.load(Ordering::Relaxed)
    }

    /// Get a summary of current risk state
    pub fn status(&self) -> RiskStatus {
        let state = self.state.lock().unwrap();
        RiskStatus {
            daily_pnl_usd: state.net_daily_pnl(),
            daily_loss_usd: state.daily_loss_usd,
            daily_profit_usd: state.daily_profit_usd,
            open_positions: state.open_positions,
            total_trades: state.total_trades,
            win_rate: state.win_rate(),
            consecutive_losses: state.consecutive_losses,
            emergency_shutdown: self.emergency_shutdown.load(Ordering::Relaxed),
        }
    }

    /// Calculate position size adjusted for current risk state
    /// Reduces size if we're on a losing streak
    pub fn adjusted_position_size(&self, base_size: Decimal) -> Decimal {
        let state = self.state.lock().unwrap();

        // Kelly-inspired position scaling
        // Reduce by 20% for each consecutive loss
        let loss_factor = Decimal::ONE
            - (Decimal::from(state.consecutive_losses) * dec!(0.2))
                .min(dec!(0.8));

        // Reduce if approaching daily loss limit
        let daily_loss_ratio = if self.config.trading.max_daily_loss_usd > Decimal::ZERO {
            state.daily_loss_usd / self.config.trading.max_daily_loss_usd
        } else {
            Decimal::ZERO
        };

        let loss_headroom_factor = if daily_loss_ratio > dec!(0.5) {
            // Linearly reduce from 100% to 20% as loss approaches limit
            dec!(1.0) - (daily_loss_ratio - dec!(0.5)) * dec!(1.6)
        } else {
            Decimal::ONE
        };

        let adjusted = base_size * loss_factor * loss_headroom_factor;
        adjusted.max(base_size * dec!(0.1)) // Never below 10% of base
    }
}

/// Public summary of risk state
#[derive(Debug, Clone)]
pub struct RiskStatus {
    pub daily_pnl_usd: Decimal,
    pub daily_loss_usd: Decimal,
    pub daily_profit_usd: Decimal,
    pub open_positions: u32,
    pub total_trades: u64,
    pub win_rate: Decimal,
    pub consecutive_losses: u32,
    pub emergency_shutdown: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::data::types::ExchangeId;
    use crate::engine::arbitrage_detector::ArbitrageOpportunity;
    use chrono::Utc;
    use rust_decimal_macros::dec;

    fn make_config() -> Arc<AppConfig> {
        use crate::config::*;
        Arc::new(AppConfig {
            server: ServerConfig { metrics_port: 9090, health_port: 8080 },
            redis: RedisConfig { url: "redis://127.0.0.1".into(), cache_ttl_ms: 500 },
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
                binance: make_exc(dec!(0.001), dec!(0.001), dec!(0.005)),
                okx: make_exc(dec!(0.0008), dec!(0.001), dec!(0.003)),
                bybit: make_exc(dec!(0.001), dec!(0.001), dec!(0.004)),
                kraken: make_exc(dec!(0.0016), dec!(0.0026), dec!(0.0035)),
            },
            dex: DexConfig {
                uniswap_v3: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
                sushiswap: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
                pancakeswap: DexExchangeConfig { enabled: false, rpc_url: "".into(), router_address: "".into(), factory_address: None, gas_limit: None },
            },
            symbols: SymbolsConfig { trading_pairs: vec!["ETH/USDT".into()], refresh_interval_ms: 200 },
            risk: RiskConfig { max_open_positions: 3, position_timeout_secs: 30, circuit_breaker_loss_pct: dec!(0.05), max_consecutive_losses: 5 },
            logging: LoggingConfig { level: "info".into(), file_path: "logs/test.log".into(), json_format: false },
        })
    }

    fn make_exc(maker: Decimal, taker: Decimal, withdrawal: Decimal) -> crate::config::ExchangeConfig {
        crate::config::ExchangeConfig {
            enabled: true,
            ws_url: "".into(),
            rest_url: "".into(),
            api_key: "".into(),
            api_secret: "".into(),
            passphrase: None,
            maker_fee: maker,
            taker_fee: taker,
            withdrawal_fee_eth: withdrawal,
        }
    }

    fn make_opp() -> ArbitrageOpportunity {
        ArbitrageOpportunity {
            id: uuid::Uuid::new_v4().to_string(),
            buy_exchange: ExchangeId::Binance,
            sell_exchange: ExchangeId::Okx,
            symbol: "ETH/USDT".into(),
            buy_price: dec!(2000),
            sell_price: dec!(2020),
            gross_spread: dec!(20),
            gross_spread_pct: dec!(0.01),
            total_fees: dec!(10),
            net_profit_usd: dec!(10),
            net_profit_pct: dec!(0.01),
            trade_size_usd: dec!(1000),
            trade_quantity: dec!(0.5),
            buy_slippage: dec!(0.0005),
            sell_slippage: dec!(0.0005),
            buy_liquidity_usd: dec!(500000),
            sell_liquidity_usd: dec!(500000),
            confidence: dec!(0.8),
            detected_at: Utc::now(),
            ttl_ms: 1000,
        }
    }

    #[test]
    fn test_valid_opportunity_passes_risk_check() {
        let rm = RiskManager::new(make_config());
        assert!(rm.check_opportunity(&make_opp()).is_ok());
    }

    #[test]
    fn test_emergency_shutdown_blocks_trading() {
        let rm = RiskManager::new(make_config());
        rm.trigger_emergency_shutdown("Test");
        assert!(rm.check_opportunity(&make_opp()).is_err());
    }

    #[test]
    fn test_daily_loss_circuit_breaker() {
        let rm = RiskManager::new(make_config());
        // Record losses exceeding the daily limit
        rm.record_trade_result(dec!(-350)); // Exceeds $300 limit
        assert!(rm.check_opportunity(&make_opp()).is_err());
    }

    #[test]
    fn test_position_size_reduction_on_losses() {
        let rm = RiskManager::new(make_config());
        rm.record_trade_result(dec!(-10));
        rm.record_trade_result(dec!(-10));
        let adjusted = rm.adjusted_position_size(dec!(5000));
        assert!(adjusted < dec!(5000));
    }
}
