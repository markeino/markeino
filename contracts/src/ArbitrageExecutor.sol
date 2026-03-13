// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// ─── Minimal interfaces (avoids heavy dependency tree) ────────────────────────

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @dev Aave V3 Pool — only the methods we need
interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16  referralCode
    ) external;
}

/// @dev Aave V3 IFlashLoanSimpleReceiver callback
interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

/// @dev Uniswap V2 Router02 (also SushiSwap)
interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @dev Uniswap V3 SwapRouter (also used for V4 until V4 SDK matures)
interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external returns (uint256 amountOut);
}

// ─── Main Contract ─────────────────────────────────────────────────────────────

/**
 * @title  ArbitrageExecutor
 * @notice Atomic flash-loan arbitrage across Uniswap V2/V3/V4 (and SushiSwap).
 *
 *  Flow:
 *   1. Owner calls executeArbitrage() with borrow details + swap params.
 *   2. Aave V3 sends `amount` of `asset` to this contract and calls
 *      executeOperation().
 *   3. executeOperation() performs two swaps (buy on cheap DEX, sell on
 *      expensive DEX).
 *   4. If profit >= minProfit the repayment approval is set; otherwise reverts
 *      (Aave will also revert — no funds are lost, only gas).
 *
 *  Deployed contract holds ZERO user funds — profit is swept to owner on each
 *  successful arbitrage via the withdraw() function, or accumulates as ERC-20
 *  balance inside the contract until manually swept.
 *
 *  Supported DEX indices (ArbParams.buyDex / sellDex):
 *    0 = Uniswap V2   (0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D)
 *    1 = Uniswap V3   (0xE592427A0AEce92De3Edee1F18E0157C05861564)
 *    2 = SushiSwap V2 (0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F)
 *    3 = Uniswap V4*  (routed via V3 SwapRouter until V4 SDK is stable)
 *
 *  V3 fee tiers: 100 (0.01%), 500 (0.05%), 3000 (0.30%), 10000 (1.00%)
 */
contract ArbitrageExecutor is IFlashLoanSimpleReceiver {

    // ── Constants ──────────────────────────────────────────────────────────────

    /// @dev Aave V3 Pool — Ethereum mainnet
    address public constant AAVE_POOL =
        0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;

    /// @dev Uniswap V2 Router02 — Ethereum mainnet
    address public constant UNI_V2_ROUTER =
        0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D;

    /// @dev Uniswap V3 SwapRouter — Ethereum mainnet
    address public constant UNI_V3_ROUTER =
        0xE592427A0AEce92De3Edee1F18E0157C05861564;

    /// @dev SushiSwap Router — Ethereum mainnet
    address public constant SUSHI_ROUTER =
        0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F;

    /// @dev WETH — Ethereum mainnet
    address public constant WETH =
        0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;

    // ── Storage ────────────────────────────────────────────────────────────────

    address public owner;

    // ── Structs ────────────────────────────────────────────────────────────────

    /**
     * @param tokenIn    Token borrowed from Aave (e.g. USDC).
     * @param tokenOut   Intermediate token to buy and sell (e.g. WETH).
     * @param buyDex     DEX index to buy tokenOut on.
     * @param sellDex    DEX index to sell tokenOut on.
     * @param buyFee     V3/V4 fee tier for the buy swap (ignored for V2).
     * @param sellFee    V3/V4 fee tier for the sell swap (ignored for V2).
     * @param minProfit  Minimum profit required (in tokenIn units); reverts if
     *                   not met, protecting against unprofitable execution.
     */
    struct ArbParams {
        address tokenIn;
        address tokenOut;
        uint8   buyDex;
        uint8   sellDex;
        uint24  buyFee;
        uint24  sellFee;
        uint256 minProfit;
    }

    // ── Events ─────────────────────────────────────────────────────────────────

    event ArbExecuted(
        address indexed asset,
        uint256 borrowed,
        uint256 profit,
        uint8   buyDex,
        uint8   sellDex
    );
    event Withdrawn(address indexed token, uint256 amount, address indexed to);

    // ── Constructor ────────────────────────────────────────────────────────────

    constructor() {
        owner = msg.sender;
    }

    // ── Modifiers ──────────────────────────────────────────────────────────────

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    // ── Owner functions ────────────────────────────────────────────────────────

    /**
     * @notice Transfer contract ownership.
     */
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero address");
        owner = newOwner;
    }

    /**
     * @notice Sweep any ERC-20 token balance out of this contract to `to`.
     */
    function withdraw(address token, address to) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "nothing to withdraw");
        IERC20(token).transfer(to, bal);
        emit Withdrawn(token, bal, to);
    }

    /**
     * @notice Convenience: withdraw to caller.
     */
    function withdrawToOwner(address token) external onlyOwner {
        this.withdraw(token, msg.sender);
    }

    // ── Entry point ────────────────────────────────────────────────────────────

    /**
     * @notice Initiate a flash-loan arbitrage.
     *
     * @param asset   Token to borrow (and return) from Aave.
     * @param amount  Amount to borrow (in token's native decimals).
     * @param params  Encoded ArbParams struct describing the two-leg swap.
     *
     * @dev  Must be called by owner. The full execution flow is atomic: if the
     *       arbitrage is unprofitable the whole transaction reverts; gas is the
     *       only cost.
     */
    function executeArbitrage(
        address asset,
        uint256 amount,
        ArbParams calldata params
    ) external onlyOwner {
        bytes memory data = abi.encode(params);
        IAavePool(AAVE_POOL).flashLoanSimple(
            address(this),
            asset,
            amount,
            data,
            0   // referral code
        );
    }

    // ── Aave callback ──────────────────────────────────────────────────────────

    /**
     * @notice Called by Aave immediately after this contract receives the loan.
     *         Must repay `amount + premium` before returning.
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == AAVE_POOL,       "caller not Aave pool");
        require(initiator  == address(this),   "bad initiator");

        ArbParams memory p = abi.decode(params, (ArbParams));
        uint256 amountOwed = amount + premium;

        // ── Leg 1: buy tokenOut with borrowed tokenIn ──────────────────────────
        uint256 received = _swap(
            p.buyDex,
            p.tokenIn,
            p.tokenOut,
            amount,
            p.buyFee,
            0          // amountOutMin — enforced by minProfit check below
        );

        // ── Leg 2: sell tokenOut back into tokenIn ─────────────────────────────
        uint256 returned = _swap(
            p.sellDex,
            p.tokenOut,
            p.tokenIn,
            received,
            p.sellFee,
            amountOwed // amountOutMin — must at least cover Aave repayment
        );

        // ── Profit gate ────────────────────────────────────────────────────────
        uint256 profit = returned - amountOwed;
        require(profit >= p.minProfit, "profit below minimum");

        // ── Approve Aave repayment ─────────────────────────────────────────────
        IERC20(asset).approve(AAVE_POOL, amountOwed);

        emit ArbExecuted(asset, amount, profit, p.buyDex, p.sellDex);
        return true;
    }

    // ── Internal swap router ───────────────────────────────────────────────────

    /**
     * @dev Route a single swap through the selected DEX.
     *
     * @param dex          0=UniV2, 1=UniV3, 2=Sushi, 3=UniV4 (via V3 router)
     * @param tokenIn      Input token address.
     * @param tokenOut     Output token address.
     * @param amountIn     Exact input amount.
     * @param fee          V3 fee tier (ignored for V2/Sushi).
     * @param amountOutMin Minimum output — revert if not met.
     * @return amountOut   Actual output received.
     */
    function _swap(
        uint8   dex,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24  fee,
        uint256 amountOutMin
    ) internal returns (uint256 amountOut) {

        if (dex == 0 || dex == 2) {
            // ── Uniswap V2 / SushiSwap ─────────────────────────────────────────
            address router = dex == 0 ? UNI_V2_ROUTER : SUSHI_ROUTER;
            IERC20(tokenIn).approve(router, amountIn);

            address[] memory path = new address[](2);
            path[0] = tokenIn;
            path[1] = tokenOut;

            uint256[] memory amounts = IUniswapV2Router(router)
                .swapExactTokensForTokens(
                    amountIn,
                    amountOutMin,
                    path,
                    address(this),
                    block.timestamp
                );
            amountOut = amounts[amounts.length - 1];

        } else if (dex == 1 || dex == 3) {
            // ── Uniswap V3 / V4 (via V3 SwapRouter) ───────────────────────────
            IERC20(tokenIn).approve(UNI_V3_ROUTER, amountIn);

            amountOut = IUniswapV3Router(UNI_V3_ROUTER).exactInputSingle(
                IUniswapV3Router.ExactInputSingleParams({
                    tokenIn:           tokenIn,
                    tokenOut:          tokenOut,
                    fee:               fee,
                    recipient:         address(this),
                    deadline:          block.timestamp,
                    amountIn:          amountIn,
                    amountOutMinimum:  amountOutMin,
                    sqrtPriceLimitX96: 0
                })
            );

        } else {
            revert("unknown dex index");
        }
    }

    // ── Safety ─────────────────────────────────────────────────────────────────

    /// @dev Reject plain ETH transfers to avoid accidental locking.
    receive() external payable {
        revert("no ETH accepted");
    }
}
