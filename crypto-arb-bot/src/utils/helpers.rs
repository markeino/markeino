use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::time::{Duration, Instant};

/// Format a Decimal as a percentage string (e.g. "1.234%")
pub fn format_pct(d: Decimal) -> String {
    format!("{:.4}%", d * dec!(100))
}

/// Format a Decimal as a USD amount (e.g. "$1,234.56")
pub fn format_usd(d: Decimal) -> String {
    let abs = d.abs();
    let sign = if d < Decimal::ZERO { "-" } else { "" };
    format!("{}${:.2}", sign, abs)
}

/// Returns how many milliseconds ago a timestamp was
pub fn ms_since(dt: DateTime<Utc>) -> i64 {
    Utc::now().signed_duration_since(dt).num_milliseconds()
}

/// Clamp a Decimal to [min, max]
pub fn clamp(val: Decimal, min: Decimal, max: Decimal) -> Decimal {
    val.max(min).min(max)
}

/// Exponential backoff helper
pub struct ExponentialBackoff {
    base_delay: Duration,
    max_delay: Duration,
    current: Duration,
    attempt: u32,
}

impl ExponentialBackoff {
    pub fn new(base_ms: u64, max_ms: u64) -> Self {
        Self {
            base_delay: Duration::from_millis(base_ms),
            max_delay: Duration::from_millis(max_ms),
            current: Duration::from_millis(base_ms),
            attempt: 0,
        }
    }

    pub async fn wait(&mut self) {
        tokio::time::sleep(self.current).await;
        self.attempt += 1;
        self.current = (self.current * 2).min(self.max_delay);
    }

    pub fn reset(&mut self) {
        self.current = self.base_delay;
        self.attempt = 0;
    }

    pub fn attempt(&self) -> u32 {
        self.attempt
    }
}

/// Rolling statistics calculator (mean, variance) for latency tracking
pub struct RollingStats {
    window: Vec<f64>,
    max_size: usize,
    sum: f64,
    sum_sq: f64,
}

impl RollingStats {
    pub fn new(window_size: usize) -> Self {
        Self {
            window: Vec::with_capacity(window_size),
            max_size: window_size,
            sum: 0.0,
            sum_sq: 0.0,
        }
    }

    pub fn push(&mut self, val: f64) {
        if self.window.len() >= self.max_size {
            let evicted = self.window.remove(0);
            self.sum -= evicted;
            self.sum_sq -= evicted * evicted;
        }
        self.window.push(val);
        self.sum += val;
        self.sum_sq += val * val;
    }

    pub fn mean(&self) -> f64 {
        if self.window.is_empty() {
            return 0.0;
        }
        self.sum / self.window.len() as f64
    }

    pub fn std_dev(&self) -> f64 {
        let n = self.window.len() as f64;
        if n < 2.0 {
            return 0.0;
        }
        let variance = (self.sum_sq - self.sum * self.sum / n) / (n - 1.0);
        variance.sqrt()
    }

    pub fn min(&self) -> f64 {
        self.window.iter().cloned().fold(f64::INFINITY, f64::min)
    }

    pub fn max(&self) -> f64 {
        self.window.iter().cloned().fold(f64::NEG_INFINITY, f64::max)
    }

    pub fn count(&self) -> usize {
        self.window.len()
    }
}

/// Simple rate limiter using token bucket algorithm
pub struct RateLimiter {
    tokens: f64,
    max_tokens: f64,
    refill_rate: f64, // tokens per second
    last_refill: Instant,
}

impl RateLimiter {
    pub fn new(requests_per_second: f64) -> Self {
        Self {
            tokens: requests_per_second,
            max_tokens: requests_per_second,
            refill_rate: requests_per_second,
            last_refill: Instant::now(),
        }
    }

    /// Try to consume a token. Returns true if allowed, false if rate limited.
    pub fn try_acquire(&mut self) -> bool {
        self.refill();
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    fn refill(&mut self) {
        let elapsed = self.last_refill.elapsed().as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.refill_rate).min(self.max_tokens);
        self.last_refill = Instant::now();
    }
}

/// Converts a symbol like "ETH/USDT" to exchange-specific format
pub fn symbol_to_exchange_format(symbol: &str, exchange: &str) -> String {
    match exchange {
        "binance" | "bybit" => symbol.replace('/', ""),
        "okx" => symbol.replace('/', "-"),
        "kraken" => symbol.replace("BTC/", "XBT/").replace("/", ""),
        _ => symbol.to_string(),
    }
}

/// Parse a trading pair into base and quote assets
pub fn parse_symbol(symbol: &str) -> Option<(String, String)> {
    let parts: Vec<&str> = symbol.split('/').collect();
    if parts.len() == 2 {
        Some((parts[0].to_string(), parts[1].to_string()))
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_format_pct() {
        assert_eq!(format_pct(dec!(0.0123)), "1.2300%");
    }

    #[test]
    fn test_format_usd() {
        assert_eq!(format_usd(dec!(1234.56)), "$1234.56");
        assert_eq!(format_usd(dec!(-50.00)), "-$50.00");
    }

    #[test]
    fn test_rolling_stats() {
        let mut stats = RollingStats::new(5);
        stats.push(1.0);
        stats.push(2.0);
        stats.push(3.0);
        assert!((stats.mean() - 2.0).abs() < 0.001);
    }

    #[test]
    fn test_parse_symbol() {
        let (base, quote) = parse_symbol("ETH/USDT").unwrap();
        assert_eq!(base, "ETH");
        assert_eq!(quote, "USDT");
    }

    #[test]
    fn test_rate_limiter() {
        let mut limiter = RateLimiter::new(10.0);
        // Should be able to acquire tokens up to the limit
        for _ in 0..10 {
            assert!(limiter.try_acquire());
        }
        // 11th should fail immediately
        assert!(!limiter.try_acquire());
    }
}
