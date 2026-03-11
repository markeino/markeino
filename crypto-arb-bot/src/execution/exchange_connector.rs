use anyhow::{Context, Result};
use async_trait::async_trait;
use base64::{engine::general_purpose, Engine};
use chrono::Utc;
use hmac::{Hmac, Mac};
use reqwest::{Client, Method};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::time::Duration;
use tracing::{debug, info, warn};

type HmacSha256 = Hmac<Sha256>;

/// Order side
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum OrderSide {
    Buy,
    Sell,
}

impl std::fmt::Display for OrderSide {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderSide::Buy => write!(f, "BUY"),
            OrderSide::Sell => write!(f, "SELL"),
        }
    }
}

/// Order type
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum OrderType {
    Market,
    Limit { price: Decimal, time_in_force: TimeInForce },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum TimeInForce {
    GoodTilCancelled,
    ImmediateOrCancel,
    FillOrKill,
}

/// Order request to be submitted to an exchange
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderRequest {
    pub client_order_id: String,
    pub symbol: String,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub quantity: Decimal,
    /// USD notional (for market orders)
    pub notional_usd: Option<Decimal>,
}

/// Order status returned from exchange
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum OrderStatus {
    New,
    PartiallyFilled,
    Filled,
    Cancelled,
    Rejected,
    Expired,
}

/// Response from order submission
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderResponse {
    pub exchange_order_id: String,
    pub client_order_id: String,
    pub symbol: String,
    pub side: OrderSide,
    pub status: OrderStatus,
    pub filled_quantity: Decimal,
    pub average_fill_price: Decimal,
    pub fee_paid: Decimal,
    pub fee_currency: String,
    pub submitted_at: chrono::DateTime<Utc>,
    pub filled_at: Option<chrono::DateTime<Utc>>,
}

/// Trait for exchange connectors (CEX and DEX)
#[async_trait]
pub trait ExchangeConnector: Send + Sync {
    fn name(&self) -> &str;

    /// Submit an order and return the initial response
    async fn submit_order(&self, request: &OrderRequest) -> Result<OrderResponse>;

    /// Query current status of an order
    async fn get_order_status(&self, exchange_order_id: &str, symbol: &str) -> Result<OrderResponse>;

    /// Cancel an open order
    async fn cancel_order(&self, exchange_order_id: &str, symbol: &str) -> Result<()>;

    /// Get current account balance for a symbol
    async fn get_balance(&self, asset: &str) -> Result<Decimal>;
}

// ─── Binance Connector ────────────────────────────────────────────────────────

pub struct BinanceConnector {
    client: Client,
    api_key: String,
    api_secret: String,
    base_url: String,
    paper_trading: bool,
}

impl BinanceConnector {
    pub fn new(api_key: String, api_secret: String, base_url: String, paper_trading: bool) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .expect("Failed to build HTTP client");

        Self {
            client,
            api_key,
            api_secret,
            base_url,
            paper_trading,
        }
    }

    fn sign(&self, query_string: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(self.api_secret.as_bytes())
            .expect("HMAC key init failed");
        mac.update(query_string.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    fn timestamp_ms() -> u64 {
        Utc::now().timestamp_millis() as u64
    }
}

#[async_trait]
impl ExchangeConnector for BinanceConnector {
    fn name(&self) -> &str {
        "binance"
    }

    async fn submit_order(&self, request: &OrderRequest) -> Result<OrderResponse> {
        if self.paper_trading {
            return Ok(simulate_paper_order(request, self.name()));
        }

        let symbol = request.symbol.replace('/', "");
        let side = request.side.to_string();
        let order_type = match &request.order_type {
            OrderType::Market => "MARKET",
            OrderType::Limit { .. } => "LIMIT",
        };

        let ts = Self::timestamp_ms();
        let mut params = format!(
            "symbol={}&side={}&type={}&quantity={}&newClientOrderId={}&timestamp={}",
            symbol, side, order_type, request.quantity, request.client_order_id, ts
        );

        if let OrderType::Limit { price, time_in_force } = &request.order_type {
            let tif = match time_in_force {
                TimeInForce::GoodTilCancelled => "GTC",
                TimeInForce::ImmediateOrCancel => "IOC",
                TimeInForce::FillOrKill => "FOK",
            };
            params += &format!("&price={}&timeInForce={}", price, tif);
        }

        let signature = self.sign(&params);
        params += &format!("&signature={}", signature);

        let url = format!("{}/api/v3/order", self.base_url);
        debug!("Submitting Binance order: {} {} {}", side, request.quantity, symbol);

        let resp = self
            .client
            .request(Method::POST, &url)
            .header("X-MBX-APIKEY", &self.api_key)
            .body(params)
            .send()
            .await
            .context("Binance order submission HTTP error")?;

        let status_code = resp.status();
        let body: serde_json::Value = resp.json().await?;

        if !status_code.is_success() {
            return Err(anyhow::anyhow!(
                "Binance order rejected ({}): {}",
                status_code,
                body
            ));
        }

        parse_binance_order_response(&body, request)
    }

    async fn get_order_status(&self, exchange_order_id: &str, symbol: &str) -> Result<OrderResponse> {
        let symbol_normalized = symbol.replace('/', "");
        let ts = Self::timestamp_ms();
        let params = format!(
            "symbol={}&orderId={}&timestamp={}",
            symbol_normalized, exchange_order_id, ts
        );
        let signature = self.sign(&params);
        let url = format!(
            "{}/api/v3/order?{}&signature={}",
            self.base_url, params, signature
        );

        let resp: serde_json::Value = self
            .client
            .get(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .send()
            .await?
            .json()
            .await?;

        parse_binance_order_status(&resp, exchange_order_id, symbol)
    }

    async fn cancel_order(&self, exchange_order_id: &str, symbol: &str) -> Result<()> {
        if self.paper_trading {
            info!("Paper trading: simulating cancel order {}", exchange_order_id);
            return Ok(());
        }

        let symbol_normalized = symbol.replace('/', "");
        let ts = Self::timestamp_ms();
        let params = format!(
            "symbol={}&orderId={}&timestamp={}",
            symbol_normalized, exchange_order_id, ts
        );
        let signature = self.sign(&params);
        let url = format!("{}/api/v3/order", self.base_url);

        self.client
            .delete(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .body(format!("{}&signature={}", params, signature))
            .send()
            .await
            .context("Binance cancel order HTTP error")?;

        Ok(())
    }

    async fn get_balance(&self, asset: &str) -> Result<Decimal> {
        if self.paper_trading {
            return Ok(Decimal::from(100000)); // Mock balance for paper trading
        }

        let ts = Self::timestamp_ms();
        let params = format!("timestamp={}", ts);
        let signature = self.sign(&params);
        let url = format!(
            "{}/api/v3/account?{}&signature={}",
            self.base_url, params, signature
        );

        let resp: serde_json::Value = self
            .client
            .get(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .send()
            .await?
            .json()
            .await?;

        let balances = resp["balances"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("No balances in response"))?;

        for bal in balances {
            if bal["asset"].as_str() == Some(asset) {
                let free: Decimal = bal["free"]
                    .as_str()
                    .unwrap_or("0")
                    .parse()
                    .unwrap_or(Decimal::ZERO);
                return Ok(free);
            }
        }

        Ok(Decimal::ZERO)
    }
}

// ─── OKX Connector ────────────────────────────────────────────────────────────

pub struct OkxConnector {
    client: Client,
    api_key: String,
    api_secret: String,
    passphrase: String,
    base_url: String,
    paper_trading: bool,
}

impl OkxConnector {
    pub fn new(
        api_key: String,
        api_secret: String,
        passphrase: String,
        base_url: String,
        paper_trading: bool,
    ) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .expect("Failed to build HTTP client");

        Self {
            client,
            api_key,
            api_secret,
            passphrase,
            base_url,
            paper_trading,
        }
    }

    fn sign(&self, timestamp: &str, method: &str, path: &str, body: &str) -> String {
        let pre_hash = format!("{}{}{}{}", timestamp, method, path, body);
        let mut mac = HmacSha256::new_from_slice(self.api_secret.as_bytes())
            .expect("HMAC key init failed");
        mac.update(pre_hash.as_bytes());
        general_purpose::STANDARD.encode(mac.finalize().into_bytes())
    }
}

#[async_trait]
impl ExchangeConnector for OkxConnector {
    fn name(&self) -> &str {
        "okx"
    }

    async fn submit_order(&self, request: &OrderRequest) -> Result<OrderResponse> {
        if self.paper_trading {
            return Ok(simulate_paper_order(request, self.name()));
        }

        let inst_id = request.symbol.replace('/', "-");
        let side = match request.side {
            OrderSide::Buy => "buy",
            OrderSide::Sell => "sell",
        };
        let ord_type = match &request.order_type {
            OrderType::Market => "market",
            OrderType::Limit { .. } => "limit",
        };

        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        let path = "/api/v5/trade/order";

        let mut body_map = serde_json::json!({
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": ord_type,
            "sz": request.quantity.to_string(),
            "clOrdId": request.client_order_id,
        });

        if let OrderType::Limit { price, .. } = &request.order_type {
            body_map["px"] = serde_json::json!(price.to_string());
        }

        let body_str = serde_json::to_string(&body_map)?;
        let signature = self.sign(&timestamp, "POST", path, &body_str);

        let url = format!("{}{}", self.base_url, path);
        let resp: serde_json::Value = self
            .client
            .post(&url)
            .header("OK-ACCESS-KEY", &self.api_key)
            .header("OK-ACCESS-SIGN", signature)
            .header("OK-ACCESS-TIMESTAMP", &timestamp)
            .header("OK-ACCESS-PASSPHRASE", &self.passphrase)
            .header("Content-Type", "application/json")
            .body(body_str)
            .send()
            .await?
            .json()
            .await?;

        if resp["code"].as_str() != Some("0") {
            return Err(anyhow::anyhow!("OKX order rejected: {}", resp));
        }

        let order_id = resp["data"][0]["ordId"]
            .as_str()
            .unwrap_or("")
            .to_string();

        Ok(OrderResponse {
            exchange_order_id: order_id,
            client_order_id: request.client_order_id.clone(),
            symbol: request.symbol.clone(),
            side: request.side.clone(),
            status: OrderStatus::New,
            filled_quantity: Decimal::ZERO,
            average_fill_price: Decimal::ZERO,
            fee_paid: Decimal::ZERO,
            fee_currency: "USDT".into(),
            submitted_at: Utc::now(),
            filled_at: None,
        })
    }

    async fn get_order_status(&self, exchange_order_id: &str, symbol: &str) -> Result<OrderResponse> {
        let inst_id = symbol.replace('/', "-");
        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        let path = format!(
            "/api/v5/trade/order?instId={}&ordId={}",
            inst_id, exchange_order_id
        );
        let signature = self.sign(&timestamp, "GET", &path, "");

        let url = format!("{}{}", self.base_url, path);
        let resp: serde_json::Value = self
            .client
            .get(&url)
            .header("OK-ACCESS-KEY", &self.api_key)
            .header("OK-ACCESS-SIGN", signature)
            .header("OK-ACCESS-TIMESTAMP", &timestamp)
            .header("OK-ACCESS-PASSPHRASE", &self.passphrase)
            .send()
            .await?
            .json()
            .await?;

        let data = &resp["data"][0];
        let status = match data["state"].as_str().unwrap_or("") {
            "filled" => OrderStatus::Filled,
            "partially_filled" => OrderStatus::PartiallyFilled,
            "cancelled" => OrderStatus::Cancelled,
            "live" => OrderStatus::New,
            _ => OrderStatus::New,
        };

        Ok(OrderResponse {
            exchange_order_id: exchange_order_id.to_string(),
            client_order_id: data["clOrdId"].as_str().unwrap_or("").to_string(),
            symbol: symbol.to_string(),
            side: if data["side"].as_str() == Some("buy") {
                OrderSide::Buy
            } else {
                OrderSide::Sell
            },
            status,
            filled_quantity: data["fillSz"].as_str().unwrap_or("0").parse().unwrap_or(Decimal::ZERO),
            average_fill_price: data["avgPx"].as_str().unwrap_or("0").parse().unwrap_or(Decimal::ZERO),
            fee_paid: Decimal::ZERO,
            fee_currency: "USDT".into(),
            submitted_at: Utc::now(),
            filled_at: None,
        })
    }

    async fn cancel_order(&self, exchange_order_id: &str, symbol: &str) -> Result<()> {
        if self.paper_trading {
            return Ok(());
        }

        let inst_id = symbol.replace('/', "-");
        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        let path = "/api/v5/trade/cancel-order";

        let body_map = serde_json::json!({
            "instId": inst_id,
            "ordId": exchange_order_id,
        });
        let body_str = serde_json::to_string(&body_map)?;
        let signature = self.sign(&timestamp, "POST", path, &body_str);

        let url = format!("{}{}", self.base_url, path);
        self.client
            .post(&url)
            .header("OK-ACCESS-KEY", &self.api_key)
            .header("OK-ACCESS-SIGN", signature)
            .header("OK-ACCESS-TIMESTAMP", &timestamp)
            .header("OK-ACCESS-PASSPHRASE", &self.passphrase)
            .header("Content-Type", "application/json")
            .body(body_str)
            .send()
            .await?;

        Ok(())
    }

    async fn get_balance(&self, asset: &str) -> Result<Decimal> {
        if self.paper_trading {
            return Ok(Decimal::from(100000));
        }

        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        let path = format!("/api/v5/account/balance?ccy={}", asset);
        let signature = self.sign(&timestamp, "GET", &path, "");

        let url = format!("{}{}", self.base_url, path);
        let resp: serde_json::Value = self
            .client
            .get(&url)
            .header("OK-ACCESS-KEY", &self.api_key)
            .header("OK-ACCESS-SIGN", signature)
            .header("OK-ACCESS-TIMESTAMP", &timestamp)
            .header("OK-ACCESS-PASSPHRASE", &self.passphrase)
            .send()
            .await?
            .json()
            .await?;

        let balance = resp["data"][0]["details"]
            .as_array()
            .and_then(|arr| arr.iter().find(|d| d["ccy"].as_str() == Some(asset)))
            .and_then(|d| d["availBal"].as_str())
            .and_then(|s| s.parse().ok())
            .unwrap_or(Decimal::ZERO);

        Ok(balance)
    }
}

// ─── Paper Trading Simulation ─────────────────────────────────────────────────

/// Simulate an order fill for paper trading mode
pub fn simulate_paper_order(request: &OrderRequest, exchange: &str) -> OrderResponse {
    let fill_price = match &request.order_type {
        OrderType::Limit { price, .. } => *price,
        OrderType::Market => {
            // Simulate small slippage for market orders
            rust_decimal_macros::dec!(0) // Would be filled at current market price
        }
    };

    info!(
        "[PAPER] {} {} {} {} on {}",
        request.side, request.quantity, request.symbol, fill_price, exchange
    );

    OrderResponse {
        exchange_order_id: format!("PAPER-{}", uuid::Uuid::new_v4()),
        client_order_id: request.client_order_id.clone(),
        symbol: request.symbol.clone(),
        side: request.side.clone(),
        status: OrderStatus::Filled,
        filled_quantity: request.quantity,
        average_fill_price: fill_price,
        fee_paid: Decimal::ZERO,
        fee_currency: "USDT".into(),
        submitted_at: Utc::now(),
        filled_at: Some(Utc::now()),
    }
}

// ─── Response Parsers ─────────────────────────────────────────────────────────

fn parse_binance_order_response(
    v: &serde_json::Value,
    request: &OrderRequest,
) -> Result<OrderResponse> {
    let order_id = v["orderId"]
        .as_u64()
        .map(|id| id.to_string())
        .unwrap_or_default();

    let status = match v["status"].as_str().unwrap_or("") {
        "FILLED" => OrderStatus::Filled,
        "PARTIALLY_FILLED" => OrderStatus::PartiallyFilled,
        "NEW" => OrderStatus::New,
        "CANCELED" => OrderStatus::Cancelled,
        "REJECTED" => OrderStatus::Rejected,
        "EXPIRED" => OrderStatus::Expired,
        _ => OrderStatus::New,
    };

    let filled_qty: Decimal = v["executedQty"]
        .as_str()
        .unwrap_or("0")
        .parse()
        .unwrap_or(Decimal::ZERO);

    let avg_price: Decimal = v["cummulativeQuoteQty"]
        .as_str()
        .and_then(|s| s.parse::<Decimal>().ok())
        .and_then(|quote_qty| {
            if filled_qty > Decimal::ZERO {
                Some(quote_qty / filled_qty)
            } else {
                None
            }
        })
        .unwrap_or(Decimal::ZERO);

    let is_filled = status == OrderStatus::Filled;
    Ok(OrderResponse {
        exchange_order_id: order_id,
        client_order_id: request.client_order_id.clone(),
        symbol: request.symbol.clone(),
        side: request.side.clone(),
        status,
        filled_quantity: filled_qty,
        average_fill_price: avg_price,
        fee_paid: Decimal::ZERO,
        fee_currency: "USDT".into(),
        submitted_at: Utc::now(),
        filled_at: if is_filled { Some(Utc::now()) } else { None },
    })
}

fn parse_binance_order_status(
    v: &serde_json::Value,
    exchange_order_id: &str,
    symbol: &str,
) -> Result<OrderResponse> {
    let status = match v["status"].as_str().unwrap_or("") {
        "FILLED" => OrderStatus::Filled,
        "PARTIALLY_FILLED" => OrderStatus::PartiallyFilled,
        "NEW" => OrderStatus::New,
        "CANCELED" => OrderStatus::Cancelled,
        "REJECTED" => OrderStatus::Rejected,
        "EXPIRED" => OrderStatus::Expired,
        _ => OrderStatus::New,
    };

    let side = if v["side"].as_str() == Some("BUY") {
        OrderSide::Buy
    } else {
        OrderSide::Sell
    };

    let filled_qty: Decimal = v["executedQty"]
        .as_str()
        .unwrap_or("0")
        .parse()
        .unwrap_or(Decimal::ZERO);

    let avg_price: Decimal = v["cummulativeQuoteQty"]
        .as_str()
        .and_then(|s| s.parse::<Decimal>().ok())
        .and_then(|quote_qty| {
            if filled_qty > Decimal::ZERO {
                Some(quote_qty / filled_qty)
            } else {
                None
            }
        })
        .unwrap_or(Decimal::ZERO);

    Ok(OrderResponse {
        exchange_order_id: exchange_order_id.to_string(),
        client_order_id: v["clientOrderId"].as_str().unwrap_or("").to_string(),
        symbol: symbol.to_string(),
        side,
        status,
        filled_quantity: filled_qty,
        average_fill_price: avg_price,
        fee_paid: Decimal::ZERO,
        fee_currency: "USDT".into(),
        submitted_at: Utc::now(),
        filled_at: None,
    })
}
