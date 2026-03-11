use anyhow::Result;
use prometheus::{
    Counter, CounterVec, Gauge, GaugeVec, Histogram, HistogramOpts, HistogramVec,
    IntGauge, Opts, Registry,
};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::sync::Arc;

/// Central metrics registry for Prometheus
pub struct Metrics {
    registry: Registry,

    // ── Trade metrics ─────────────────────────────────────────────────────────
    pub trades_total: Counter,
    pub trades_successful: Counter,
    pub trades_failed: Counter,
    pub trade_profit_usd: Gauge,
    pub daily_pnl_usd: Gauge,

    // ── Latency metrics ───────────────────────────────────────────────────────
    pub opportunity_detection_latency_ms: Histogram,
    pub order_submission_latency_ms: HistogramVec,
    pub full_trade_latency_ms: Histogram,

    // ── Market data metrics ───────────────────────────────────────────────────
    pub price_updates_total: CounterVec,
    pub websocket_reconnects: CounterVec,
    pub price_staleness_ms: GaugeVec,

    // ── Opportunity metrics ───────────────────────────────────────────────────
    pub opportunities_detected: Counter,
    pub opportunities_executed: Counter,
    pub opportunities_rejected_risk: Counter,
    pub best_spread_pct: Gauge,

    // ── System metrics ────────────────────────────────────────────────────────
    pub open_positions: IntGauge,
    pub daily_loss_usd: Gauge,
    pub consecutive_losses: IntGauge,
}

impl Metrics {
    pub fn new() -> Result<Arc<Self>> {
        let registry = Registry::new();

        let trades_total = Counter::with_opts(Opts::new("arb_trades_total", "Total trades attempted"))?;
        let trades_successful = Counter::with_opts(Opts::new("arb_trades_successful_total", "Successful trades"))?;
        let trades_failed = Counter::with_opts(Opts::new("arb_trades_failed_total", "Failed trades"))?;
        let trade_profit_usd = Gauge::with_opts(Opts::new("arb_last_trade_profit_usd", "Last trade profit in USD"))?;
        let daily_pnl_usd = Gauge::with_opts(Opts::new("arb_daily_pnl_usd", "Daily PnL in USD"))?;

        let opp_latency = Histogram::with_opts(
            HistogramOpts::new(
                "arb_opportunity_detection_latency_ms",
                "Time to detect opportunity from data update",
            )
            .buckets(vec![1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0]),
        )?;

        let order_latency = HistogramVec::new(
            HistogramOpts::new("arb_order_submission_latency_ms", "Order submission latency by exchange")
                .buckets(vec![10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0]),
            &["exchange"],
        )?;

        let trade_latency = Histogram::with_opts(
            HistogramOpts::new("arb_full_trade_latency_ms", "Full round-trip trade latency")
                .buckets(vec![100.0, 250.0, 500.0, 1000.0, 2000.0, 5000.0]),
        )?;

        let price_updates = CounterVec::new(
            Opts::new("arb_price_updates_total", "Price update count by exchange"),
            &["exchange"],
        )?;

        let ws_reconnects = CounterVec::new(
            Opts::new("arb_websocket_reconnects_total", "WebSocket reconnection count by exchange"),
            &["exchange"],
        )?;

        let staleness = GaugeVec::new(
            Opts::new("arb_price_staleness_ms", "Age of most recent price data by exchange"),
            &["exchange"],
        )?;

        let opps_detected = Counter::with_opts(Opts::new("arb_opportunities_detected_total", "Detected opportunities"))?;
        let opps_executed = Counter::with_opts(Opts::new("arb_opportunities_executed_total", "Executed opportunities"))?;
        let opps_rejected = Counter::with_opts(Opts::new("arb_opportunities_rejected_risk_total", "Opportunities rejected by risk manager"))?;
        let best_spread = Gauge::with_opts(Opts::new("arb_best_spread_pct", "Best spread seen in last scan"))?;

        let open_pos = IntGauge::with_opts(Opts::new("arb_open_positions", "Currently open positions"))?;
        let daily_loss = Gauge::with_opts(Opts::new("arb_daily_loss_usd", "Daily loss accumulated in USD"))?;
        let consec_losses = IntGauge::with_opts(Opts::new("arb_consecutive_losses", "Consecutive losing trades"))?;

        // Register all metrics
        registry.register(Box::new(trades_total.clone()))?;
        registry.register(Box::new(trades_successful.clone()))?;
        registry.register(Box::new(trades_failed.clone()))?;
        registry.register(Box::new(trade_profit_usd.clone()))?;
        registry.register(Box::new(daily_pnl_usd.clone()))?;
        registry.register(Box::new(opp_latency.clone()))?;
        registry.register(Box::new(order_latency.clone()))?;
        registry.register(Box::new(trade_latency.clone()))?;
        registry.register(Box::new(price_updates.clone()))?;
        registry.register(Box::new(ws_reconnects.clone()))?;
        registry.register(Box::new(staleness.clone()))?;
        registry.register(Box::new(opps_detected.clone()))?;
        registry.register(Box::new(opps_executed.clone()))?;
        registry.register(Box::new(opps_rejected.clone()))?;
        registry.register(Box::new(best_spread.clone()))?;
        registry.register(Box::new(open_pos.clone()))?;
        registry.register(Box::new(daily_loss.clone()))?;
        registry.register(Box::new(consec_losses.clone()))?;

        Ok(Arc::new(Self {
            registry,
            trades_total,
            trades_successful,
            trades_failed,
            trade_profit_usd,
            daily_pnl_usd,
            opportunity_detection_latency_ms: opp_latency,
            order_submission_latency_ms: order_latency,
            full_trade_latency_ms: trade_latency,
            price_updates_total: price_updates,
            websocket_reconnects: ws_reconnects,
            price_staleness_ms: staleness,
            opportunities_detected: opps_detected,
            opportunities_executed: opps_executed,
            opportunities_rejected_risk: opps_rejected,
            best_spread_pct: best_spread,
            open_positions: open_pos,
            daily_loss_usd: daily_loss,
            consecutive_losses: consec_losses,
        }))
    }

    pub fn record_trade_completed(&self, pnl: Decimal) {
        self.trades_total.inc();
        self.trades_successful.inc();
        self.trade_profit_usd.set(pnl.to_f64().unwrap_or(0.0));

        let current_daily = self.daily_pnl_usd.get();
        self.daily_pnl_usd
            .set(current_daily + pnl.to_f64().unwrap_or(0.0));
    }

    pub fn record_trade_failed(&self) {
        self.trades_total.inc();
        self.trades_failed.inc();
    }

    pub fn record_opportunity_detected(&self) {
        self.opportunities_detected.inc();
    }

    pub fn record_opportunity_executed(&self) {
        self.opportunities_executed.inc();
    }

    pub fn record_opportunity_rejected_risk(&self) {
        self.opportunities_rejected_risk.inc();
    }

    pub fn record_price_update(&self, exchange: &str) {
        self.price_updates_total
            .with_label_values(&[exchange])
            .inc();
    }

    pub fn record_ws_reconnect(&self, exchange: &str) {
        self.websocket_reconnects
            .with_label_values(&[exchange])
            .inc();
    }

    pub fn set_best_spread(&self, spread_pct: f64) {
        self.best_spread_pct.set(spread_pct);
    }

    pub fn set_open_positions(&self, count: i64) {
        self.open_positions.set(count);
    }

    pub fn update_risk_state(&self, daily_loss: f64, consec_losses: i64) {
        self.daily_loss_usd.set(daily_loss);
        self.consecutive_losses.set(consec_losses);
    }

    /// Render metrics in Prometheus text exposition format
    pub fn gather(&self) -> String {
        use prometheus::Encoder;
        let encoder = prometheus::TextEncoder::new();
        let mut buffer = Vec::new();
        let metric_families = self.registry.gather();
        encoder
            .encode(&metric_families, &mut buffer)
            .unwrap_or_default();
        String::from_utf8(buffer).unwrap_or_default()
    }
}

/// Timing helper for measuring latency
pub struct Timer {
    start: std::time::Instant,
}

impl Timer {
    pub fn start() -> Self {
        Self {
            start: std::time::Instant::now(),
        }
    }

    pub fn elapsed_ms(&self) -> f64 {
        self.start.elapsed().as_secs_f64() * 1000.0
    }
}
