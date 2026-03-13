"""
flash_executor.py
=================
Python interface to ArbitrageExecutor.sol.

Called by dex_arb_v4.py when a profitable spread is detected.  Builds,
simulates, and (optionally) broadcasts a flash-loan arbitrage transaction.

Environment variables (set in .env or shell):
  ETH_RPC_URL          Ethereum JSON-RPC endpoint (Alchemy / Infura / local)
  PRIVATE_KEY          Hex private key of the executor wallet (for signing)
  ARB_CONTRACT_ADDR    Deployed ArbitrageExecutor contract address
  FLASHBOTS_RPC_URL    (optional) Flashbots relay for MEV protection
  DRY_RUN              Set to "1" to simulate only — never broadcast (default: "0")
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError
from eth_account import Account

# ─── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger("flash_executor")
if not log.handlers:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

# ─── DEX index mapping ────────────────────────────────────────────────────────
# Must match ArbitrageExecutor.sol constants
DEX_INDEX: dict[str, int] = {
    "uniswap_v2": 0,
    "uniswap_v3": 1,
    "sushiswap":  2,
    "uniswap_v4": 3,   # routed via V3 SwapRouter in contract
}

# V3 fee tiers (in bps).  500 = 0.05%, 3000 = 0.30%
_V3_STABLE_FEE = 500
_V3_DEFAULT_FEE = 3000

# Token decimals for computing borrow amounts
TOKEN_DECIMALS: dict[str, int] = {
    "USDC": 6,
    "USDT": 6,
    "DAI":  18,
    "WETH": 18,
    "WBTC": 8,
    "LINK": 18,
    "UNI":  18,
    "AAVE": 18,
    "LDO":  18,
    "PEPE": 18,
    "MKR":  18,
}

# Token contract addresses (Ethereum mainnet)
TOKEN_ADDRESSES: dict[str, str] = {
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "UNI":  "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "LDO":  "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",
    "PEPE": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
    "MKR":  "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
}

# Minimal ABI — only executeArbitrage and events we care about
_CONTRACT_ABI = json.loads("""[
  {
    "name": "executeArbitrage",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
      {"name": "asset",  "type": "address"},
      {"name": "amount", "type": "uint256"},
      {
        "name": "params",
        "type": "tuple",
        "components": [
          {"name": "tokenIn",   "type": "address"},
          {"name": "tokenOut",  "type": "address"},
          {"name": "buyDex",    "type": "uint8"},
          {"name": "sellDex",   "type": "uint8"},
          {"name": "buyFee",    "type": "uint24"},
          {"name": "sellFee",   "type": "uint24"},
          {"name": "minProfit", "type": "uint256"}
        ]
      }
    ],
    "outputs": []
  },
  {
    "name": "ArbExecuted",
    "type": "event",
    "inputs": [
      {"name": "asset",    "type": "address", "indexed": true},
      {"name": "borrowed", "type": "uint256", "indexed": false},
      {"name": "profit",   "type": "uint256", "indexed": false},
      {"name": "buyDex",   "type": "uint8",   "indexed": false},
      {"name": "sellDex",  "type": "uint8",   "indexed": false}
    ]
  },
  {
    "name": "owner",
    "type": "function",
    "stateMutability": "view",
    "inputs":  [],
    "outputs": [{"name": "", "type": "address"}]
  }
]""")


# ─── Config dataclass ─────────────────────────────────────────────────────────

@dataclass
class ExecutorConfig:
    rpc_url:           str
    private_key:       str
    contract_address:  str
    flashbots_rpc_url: str  = ""
    dry_run:           bool = False
    gas_limit:         int  = 600_000
    max_fee_gwei:      float = 50.0      # abort if base fee > this
    priority_fee_gwei: float = 2.0
    flash_loan_usdt:   float = 50_000.0  # default borrow size
    min_profit_usdt:   float = 15.0      # minimum profit before executing


def config_from_env() -> ExecutorConfig:
    """Build ExecutorConfig from environment variables."""
    return ExecutorConfig(
        rpc_url          = os.environ["ETH_RPC_URL"],
        private_key      = os.environ["PRIVATE_KEY"],
        contract_address = os.environ["ARB_CONTRACT_ADDR"],
        flashbots_rpc_url= os.getenv("FLASHBOTS_RPC_URL", ""),
        dry_run          = os.getenv("DRY_RUN", "0") == "1",
        gas_limit        = int(os.getenv("GAS_LIMIT", "600000")),
        max_fee_gwei     = float(os.getenv("MAX_FEE_GWEI", "50")),
        priority_fee_gwei= float(os.getenv("PRIORITY_FEE_GWEI", "2")),
        flash_loan_usdt  = float(os.getenv("FLASH_LOAN_USDT", "50000")),
        min_profit_usdt  = float(os.getenv("MIN_PROFIT_USDT", "15")),
    )


# ─── FlashExecutor ────────────────────────────────────────────────────────────

class FlashExecutor:
    """
    Stateless helper that wraps web3.py calls to ArbitrageExecutor.sol.

    Example usage:
        executor = FlashExecutor(config_from_env())
        tx_hash  = executor.trigger("ETH/USDC", "uniswap_v2", "uniswap_v3",
                                     buy_price=2450.0, sell_price=2452.0)
    """

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg  = cfg
        self._w3  = Web3(Web3.HTTPProvider(cfg.rpc_url))
        self._acc = Account.from_key(cfg.private_key)

        addr = Web3.to_checksum_address(cfg.contract_address)
        self._contract = self._w3.eth.contract(address=addr, abi=_CONTRACT_ABI)

        log.info("FlashExecutor ready  wallet=%s  contract=%s  dry_run=%s",
                 self._acc.address, addr, cfg.dry_run)

    # ── Public API ─────────────────────────────────────────────────────────────

    def trigger(
        self,
        pair:       str,
        buy_dex:    str,
        sell_dex:   str,
        buy_price:  float,
        sell_price: float,
        trade_usdt: Optional[float] = None,
        min_profit: Optional[float] = None,
    ) -> Optional[str]:
        """
        Attempt to execute a flash-loan arbitrage for the given opportunity.

        Parameters
        ----------
        pair        Trading pair, e.g. "ETH/USDC"
        buy_dex     DEX name to buy on (cheaper), e.g. "uniswap_v2"
        sell_dex    DEX name to sell on (pricier), e.g. "uniswap_v3"
        buy_price   Current buy-side price (USD per ETH)
        sell_price  Current sell-side price (USD per ETH)
        trade_usdt  Flash-loan borrow size in USD (default: cfg.flash_loan_usdt)
        min_profit  Minimum acceptable profit in USD (default: cfg.min_profit_usdt)

        Returns
        -------
        tx_hash (str) on success, None on simulation failure or dry-run.
        """
        trade_usdt = trade_usdt or self.cfg.flash_loan_usdt
        min_profit = min_profit or self.cfg.min_profit_usdt

        # ── Rough profit estimate (pre-flight) ─────────────────────────────────
        gross_pct   = (sell_price - buy_price) / buy_price * 100
        est_profit  = (trade_usdt / buy_price * sell_price * 0.997) - (trade_usdt * 1.003)
        aave_fee    = trade_usdt * 0.0005  # 0.05%
        net_est     = est_profit - aave_fee

        log.info("opportunity  pair=%s  buy=%s@%.4f  sell=%s@%.4f  "
                 "gross=%.4f%%  est_net=$%.2f",
                 pair, buy_dex, buy_price, sell_dex, sell_price,
                 gross_pct, net_est)

        if net_est < min_profit:
            log.info("skipping — est_net $%.2f < min_profit $%.2f", net_est, min_profit)
            return None

        # ── Gas sanity check ───────────────────────────────────────────────────
        base_fee_gwei = self._w3.eth.gas_price / 1e9
        if base_fee_gwei > self.cfg.max_fee_gwei:
            log.warning("gas too high: %.1f gwei > max %.1f — skipping",
                        base_fee_gwei, self.cfg.max_fee_gwei)
            return None

        # ── Build tx params ────────────────────────────────────────────────────
        base, quote = pair.split("/")
        borrow_token  = Web3.to_checksum_address(TOKEN_ADDRESSES[quote])
        target_token  = Web3.to_checksum_address(
            TOKEN_ADDRESSES["WETH"] if base == "ETH" else TOKEN_ADDRESSES[base]
        )
        dec           = TOKEN_DECIMALS.get(quote, 18)
        borrow_amount = int(trade_usdt * 10**dec)
        min_profit_tk = int(min_profit * 10**dec)

        arb_params = {
            "tokenIn":   borrow_token,
            "tokenOut":  target_token,
            "buyDex":    DEX_INDEX.get(buy_dex, 0),
            "sellDex":   DEX_INDEX.get(sell_dex, 1),
            "buyFee":    self._v3_fee(buy_dex, pair),
            "sellFee":   self._v3_fee(sell_dex, pair),
            "minProfit": min_profit_tk,
        }

        # ── Simulate (eth_call) before sending ────────────────────────────────
        if not self._simulate(borrow_token, borrow_amount, arb_params):
            return None

        if self.cfg.dry_run:
            log.info("DRY_RUN=1 — not broadcasting")
            return None

        # ── Build and sign transaction ─────────────────────────────────────────
        return self._send(borrow_token, borrow_amount, arb_params, base_fee_gwei)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _simulate(
        self,
        asset:        str,
        amount:       int,
        arb_params:   dict,
    ) -> bool:
        """eth_call simulation — returns True only if it would succeed."""
        try:
            self._contract.functions.executeArbitrage(
                asset, amount, arb_params
            ).call({"from": self._acc.address})
            log.info("simulation OK")
            return True
        except ContractLogicError as e:
            log.warning("simulation reverted: %s", e)
            return False
        except Exception as e:
            log.warning("simulation error: %s", e)
            return False

    def _send(
        self,
        asset:          str,
        amount:         int,
        arb_params:     dict,
        base_fee_gwei:  float,
    ) -> Optional[str]:
        """Sign and broadcast the transaction."""
        priority = int(self.cfg.priority_fee_gwei * 1e9)
        max_fee  = int((base_fee_gwei * 1.5 + self.cfg.priority_fee_gwei) * 1e9)

        try:
            nonce = self._w3.eth.get_transaction_count(self._acc.address, "pending")

            tx = self._contract.functions.executeArbitrage(
                asset, amount, arb_params
            ).build_transaction({
                "from":                  self._acc.address,
                "gas":                   self.cfg.gas_limit,
                "maxFeePerGas":          max_fee,
                "maxPriorityFeePerGas":  priority,
                "nonce":                 nonce,
                "chainId":               1,
            })

            signed   = self._acc.sign_transaction(tx)
            rpc      = self.cfg.flashbots_rpc_url or self.cfg.rpc_url
            w3_send  = Web3(Web3.HTTPProvider(rpc)) if rpc != self.cfg.rpc_url else self._w3

            tx_hash  = w3_send.eth.send_raw_transaction(signed.raw_transaction)
            hex_hash = tx_hash.hex()
            log.info("tx sent  hash=%s", hex_hash)
            return hex_hash

        except Exception as e:
            log.error("send failed: %s", e)
            return None

    @staticmethod
    def _v3_fee(dex: str, pair: str) -> int:
        """Return Uniswap V3 fee tier (bps) appropriate for the pair/dex."""
        if dex in ("uniswap_v2", "sushiswap"):
            return _V3_DEFAULT_FEE  # ignored by contract for V2
        quote = pair.split("/")[1]
        return _V3_STABLE_FEE if quote in ("USDC", "USDT", "DAI") else _V3_DEFAULT_FEE


# ─── Module-level singleton ───────────────────────────────────────────────────

_executor: Optional[FlashExecutor] = None

def get_executor() -> Optional[FlashExecutor]:
    """
    Return the module-level FlashExecutor, initialising from env on first call.
    Returns None (with a warning) if env vars are not configured.
    """
    global _executor
    if _executor is not None:
        return _executor

    required = ("ETH_RPC_URL", "PRIVATE_KEY", "ARB_CONTRACT_ADDR")
    missing  = [k for k in required if not os.getenv(k)]
    if missing:
        log.warning(
            "Flash-loan execution disabled — missing env vars: %s",
            ", ".join(missing),
        )
        return None

    try:
        _executor = FlashExecutor(config_from_env())
    except Exception as e:
        log.error("Failed to init FlashExecutor: %s", e)
        return None

    return _executor
