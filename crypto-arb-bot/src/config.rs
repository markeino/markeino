use anyhow::Result;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Top-level application configuration
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub redis: RedisConfig,
    pub trading: TradingConfig,
    pub exchanges: ExchangesConfig,
    pub dex: DexConfig,
    pub symbols: SymbolsConfig,
    pub risk: RiskConfig,
    pub logging: LoggingConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ServerConfig {
    pub metrics_port: u16,
    pub health_port: u16,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RedisConfig {
    pub url: String,
    pub cache_ttl_ms: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TradingConfig {
    pub min_profit_threshold: Decimal,
    pub max_trade_size_usd: Decimal,
    pub max_daily_loss_usd: Decimal,
    pub min_liquidity_usd: Decimal,
    pub max_slippage: Decimal,
    pub paper_trading: bool,
    pub base_currency: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ExchangesConfig {
    pub binance: ExchangeConfig,
    pub okx: ExchangeConfig,
    pub bybit: ExchangeConfig,
    pub kraken: ExchangeConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ExchangeConfig {
    pub enabled: bool,
    pub ws_url: String,
    pub rest_url: String,
    pub api_key: String,
    pub api_secret: String,
    #[serde(default)]
    pub passphrase: Option<String>,
    pub maker_fee: Decimal,
    pub taker_fee: Decimal,
    pub withdrawal_fee_eth: Decimal,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DexConfig {
    pub uniswap_v3: DexExchangeConfig,
    pub sushiswap: DexExchangeConfig,
    pub pancakeswap: DexExchangeConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DexExchangeConfig {
    pub enabled: bool,
    pub rpc_url: String,
    pub router_address: String,
    #[serde(default)]
    pub factory_address: Option<String>,
    #[serde(default)]
    pub gas_limit: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SymbolsConfig {
    pub trading_pairs: Vec<String>,
    pub refresh_interval_ms: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RiskConfig {
    pub max_open_positions: u32,
    pub position_timeout_secs: u64,
    pub circuit_breaker_loss_pct: Decimal,
    pub max_consecutive_losses: u32,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LoggingConfig {
    pub level: String,
    pub file_path: String,
    pub json_format: bool,
}

impl AppConfig {
    /// Load configuration from files and environment variables.
    /// Environment variables prefixed with `ARB_` override file settings.
    /// E.g., `ARB_TRADING__API_KEY=xxx` overrides `trading.api_key`.
    pub fn load() -> Result<Self> {
        let settings = config::Config::builder()
            .add_source(config::File::with_name("config/default").required(false))
            .add_source(config::File::with_name("config/local").required(false))
            .add_source(
                config::Environment::with_prefix("ARB")
                    .separator("__")
                    .try_parsing(true),
            )
            .build()?;

        let config: AppConfig = settings.try_deserialize()?;
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        use anyhow::bail;

        if self.trading.min_profit_threshold <= Decimal::ZERO {
            bail!("min_profit_threshold must be > 0");
        }
        if self.trading.max_trade_size_usd <= Decimal::ZERO {
            bail!("max_trade_size_usd must be > 0");
        }
        if self.trading.max_slippage <= Decimal::ZERO {
            bail!("max_slippage must be > 0");
        }
        if self.symbols.trading_pairs.is_empty() {
            bail!("trading_pairs must not be empty");
        }

        Ok(())
    }

    /// Returns a map of exchange name -> ExchangeConfig for enabled CEX exchanges
    pub fn enabled_cex_exchanges(&self) -> HashMap<String, &ExchangeConfig> {
        let mut map = HashMap::new();
        if self.exchanges.binance.enabled {
            map.insert("binance".to_string(), &self.exchanges.binance);
        }
        if self.exchanges.okx.enabled {
            map.insert("okx".to_string(), &self.exchanges.okx);
        }
        if self.exchanges.bybit.enabled {
            map.insert("bybit".to_string(), &self.exchanges.bybit);
        }
        if self.exchanges.kraken.enabled {
            map.insert("kraken".to_string(), &self.exchanges.kraken);
        }
        map
    }
}
