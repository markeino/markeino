// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/ArbitrageExecutor.sol";

/**
 * @title  Deploy
 * @notice Foundry deploy script for ArbitrageExecutor.
 *
 * Usage (dry-run, no broadcast):
 *   forge script script/Deploy.s.sol --rpc-url mainnet -vvvv
 *
 * Usage (live broadcast):
 *   forge script script/Deploy.s.sol \
 *     --rpc-url mainnet \
 *     --private-key $PRIVATE_KEY \
 *     --broadcast \
 *     --verify \
 *     --etherscan-api-key $ETHERSCAN_API_KEY \
 *     -vvvv
 *
 * After deployment the contract address is printed and also saved in the
 * broadcast/ directory as broadcast/Deploy.s.sol/1/run-latest.json.
 */
contract Deploy is Script {
    function run() external returns (ArbitrageExecutor executor) {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer    = vm.addr(deployerKey);

        console.log("Deployer  :", deployer);
        console.log("Chain ID  :", block.chainid);

        vm.startBroadcast(deployerKey);
        executor = new ArbitrageExecutor();
        vm.stopBroadcast();

        console.log("ArbitrageExecutor deployed at:", address(executor));
        console.log("Owner                        :", executor.owner());
    }
}
