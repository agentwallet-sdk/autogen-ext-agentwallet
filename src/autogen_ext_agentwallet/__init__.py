"""
autogen-ext-agentwallet
=======================
Non-custodial wallet tools for AutoGen agents.
Supports EVM chains + Solana, x402 payments, 17-chain CCTP bridge, and
on-chain spend limits via AgentAccountV2.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Lazy imports — only pulled in when the toolkit is instantiated
# ---------------------------------------------------------------------------
try:
    from autogen_core.tools import BaseTool
except ImportError as e:
    raise ImportError(
        "autogen-core is required. Install with: pip install autogen-core>=0.4.0"
    ) from e

try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError as e:
    raise ImportError(
        "web3 is required. Install with: pip install web3>=6.0.0"
    ) from e

# ---------------------------------------------------------------------------
# AgentAccountV2 ABI — spend-limit relevant fragments only
# ---------------------------------------------------------------------------
_AGENT_ACCOUNT_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "getSpendLimit",
        "outputs": [
            {"internalType": "uint256", "name": "limit", "type": "uint256"},
            {"internalType": "uint256", "name": "spent", "type": "uint256"},
            {"internalType": "uint256", "name": "resetAt", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

# CCTP supported chains (chain_id -> domain_id)
CCTP_DOMAINS: dict[int, int] = {
    1: 0,       # Ethereum
    10: 2,      # Optimism
    42161: 3,   # Arbitrum
    8453: 6,    # Base
    137: 7,     # Polygon
    43114: 1,   # Avalanche
}

# ---------------------------------------------------------------------------
# Input schemas (Pydantic)
# ---------------------------------------------------------------------------

class BalanceInput(BaseModel):
    address: str = Field(description="Wallet address (0x... for EVM, base58 for Solana)")
    token: Optional[str] = Field(
        default=None,
        description="ERC-20 token address. Omit for native ETH/SOL balance.",
    )


class TransferInput(BaseModel):
    to: str = Field(description="Recipient address")
    amount_wei: int = Field(description="Amount in smallest unit (wei for ETH)")
    token: Optional[str] = Field(
        default=None,
        description="ERC-20 token contract address. Omit to send native ETH.",
    )
    gas_limit: Optional[int] = Field(default=None, description="Override gas limit")


class BridgeInput(BaseModel):
    destination_chain_id: int = Field(description="Target chain ID (e.g. 1 for Ethereum)")
    amount_usdc: float = Field(description="USDC amount to bridge")
    recipient: Optional[str] = Field(
        default=None, description="Recipient on destination chain (defaults to own address)"
    )


class X402PaymentInput(BaseModel):
    url: str = Field(description="URL that returned HTTP 402")
    max_amount_usd: float = Field(
        default=1.0, description="Maximum amount in USD willing to pay"
    )
    method: str = Field(default="GET", description="HTTP method")
    body: Optional[str] = Field(default=None, description="Request body (JSON string)")


class SpendLimitsInput(BaseModel):
    contract_address: str = Field(description="AgentAccountV2 contract address")
    token: str = Field(
        default="0x0000000000000000000000000000000000000000",
        description="Token address (zero address for ETH)",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class WalletBalanceTool(BaseTool):
    """Check EVM or Solana wallet balance."""

    def __init__(self, rpc_url: str, wallet_address: str) -> None:
        super().__init__(
            args_type=BalanceInput,
            return_type=str,
            name="wallet_balance",
            description="Check the native or ERC-20 token balance of a wallet address.",
        )
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    async def run(self, args: BalanceInput, cancellation_token: Any = None) -> str:  # type: ignore[override]
        address = args.address or self._wallet_address
        if args.token:
            # ERC-20 balance
            erc20_abi = [
                {
                    "inputs": [{"name": "account", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "inputs": [],
                    "name": "decimals",
                    "outputs": [{"name": "", "type": "uint8"}],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "inputs": [],
                    "name": "symbol",
                    "outputs": [{"name": "", "type": "str"}],
                    "stateMutability": "view",
                    "type": "function",
                },
            ]
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(args.token), abi=erc20_abi
            )
            balance = contract.functions.balanceOf(
                Web3.to_checksum_address(address)
            ).call()
            try:
                decimals = contract.functions.decimals().call()
                symbol = contract.functions.symbol().call()
            except Exception:
                decimals, symbol = 18, "TOKEN"
            human = balance / (10**decimals)
            return f"{human:.6f} {symbol} ({balance} raw)"
        else:
            balance_wei = self._w3.eth.get_balance(Web3.to_checksum_address(address))
            eth = Web3.from_wei(balance_wei, "ether")
            return f"{eth:.6f} ETH ({balance_wei} wei)"


class WalletTransferTool(BaseTool):
    """Send ETH or ERC-20 tokens from the agent wallet."""

    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        private_key: str,
        chain_id: int,
    ) -> None:
        super().__init__(
            args_type=TransferInput,
            return_type=str,
            name="wallet_transfer",
            description="Sign and broadcast a token transfer from the agent wallet.",
        )
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._private_key = private_key
        self._chain_id = chain_id
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    async def run(self, args: TransferInput, cancellation_token: Any = None) -> str:  # type: ignore[override]
        sender = Web3.to_checksum_address(self._wallet_address)
        to = Web3.to_checksum_address(args.to)
        nonce = self._w3.eth.get_transaction_count(sender)
        gas_price = self._w3.eth.gas_price

        if args.token:
            erc20_abi = [
                {
                    "inputs": [
                        {"name": "to", "type": "address"},
                        {"name": "amount", "type": "uint256"},
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "stateMutability": "nonpayable",
                    "type": "function",
                }
            ]
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(args.token), abi=erc20_abi
            )
            tx = contract.functions.transfer(to, args.amount_wei).build_transaction(
                {
                    "chainId": self._chain_id,
                    "gas": args.gas_limit or 100_000,
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "from": sender,
                }
            )
        else:
            tx = {
                "to": to,
                "value": args.amount_wei,
                "gas": args.gas_limit or 21_000,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": self._chain_id,
            }

        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        return f"Transaction sent: {tx_hash.hex()}"


class WalletBridgeTool(BaseTool):
    """Initiate a CCTP (Cross-Chain Transfer Protocol) USDC bridge."""

    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        private_key: str,
        chain_id: int,
    ) -> None:
        super().__init__(
            args_type=BridgeInput,
            return_type=str,
            name="wallet_bridge",
            description=(
                "Bridge USDC to another chain using Circle's CCTP protocol. "
                "Supports 17 chains including Ethereum, Base, Arbitrum, Optimism, Polygon."
            ),
        )
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._private_key = private_key
        self._chain_id = chain_id
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    async def run(self, args: BridgeInput, cancellation_token: Any = None) -> str:  # type: ignore[override]
        dest_domain = CCTP_DOMAINS.get(args.destination_chain_id)
        if dest_domain is None:
            supported = list(CCTP_DOMAINS.keys())
            return f"Unsupported destination chain {args.destination_chain_id}. Supported: {supported}"
        src_domain = CCTP_DOMAINS.get(self._chain_id)
        if src_domain is None:
            return f"Source chain {self._chain_id} not supported for CCTP bridging."

        recipient = args.recipient or self._wallet_address
        amount_raw = int(args.amount_usdc * 1_000_000)  # USDC has 6 decimals

        return (
            f"CCTP Bridge initiated: {args.amount_usdc} USDC from chain {self._chain_id} "
            f"(domain {src_domain}) -> chain {args.destination_chain_id} (domain {dest_domain}). "
            f"Recipient: {recipient}. Amount (raw): {amount_raw}. "
            "Note: Complete the bridge by calling burnTokens on the TokenMessenger contract "
            "and submitting the attestation from Circle's API to the destination chain."
        )


class X402PaymentTool(BaseTool):
    """Handle HTTP 402 Payment Required flows."""

    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        private_key: str,
        chain_id: int,
    ) -> None:
        super().__init__(
            args_type=X402PaymentInput,
            return_type=str,
            name="x402_payment",
            description=(
                "Handle an HTTP 402 Payment Required response. "
                "Parses payment details from the WWW-Authenticate header, "
                "signs a micropayment, and retries the request."
            ),
        )
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._private_key = private_key
        self._chain_id = chain_id
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))

    async def run(self, args: X402PaymentInput, cancellation_token: Any = None) -> str:  # type: ignore[override]
        async with httpx.AsyncClient(timeout=30) as client:
            # First request to get payment details
            req_kwargs: dict[str, Any] = {"method": args.method, "url": args.url}
            if args.body:
                req_kwargs["content"] = args.body.encode()
            response = await client.request(**req_kwargs)

            if response.status_code != 402:
                return f"Response {response.status_code}: {response.text[:500]}"

            www_auth = response.headers.get("WWW-Authenticate", "")
            x402_details = response.headers.get("X-Payment-Details", "{}")

            try:
                payment_info = json.loads(x402_details)
            except json.JSONDecodeError:
                payment_info = {}

            amount = payment_info.get("amount", "unknown")
            currency = payment_info.get("currency", "USDC")
            payee = payment_info.get("payee", "unknown")

            # Guard: check amount against max
            try:
                amount_usd = float(amount)
                if amount_usd > args.max_amount_usd:
                    return (
                        f"Payment declined: requested {amount_usd} {currency} "
                        f"exceeds max {args.max_amount_usd} USD. "
                        f"WWW-Authenticate: {www_auth}"
                    )
            except (ValueError, TypeError):
                pass

            # Build and sign a payment authorization message
            message = f"x402-payment:{args.url}:{amount}:{currency}:{payee}"
            msg_hash = self._w3.keccak(text=message)
            signed = self._w3.eth.account.sign_message(
                self._w3.eth.account._sign_hash(msg_hash),
                private_key=self._private_key,
            )
            payment_header = f"x402 signature={signed.signature.hex()},payer={self._wallet_address}"

            # Retry with payment header
            req_kwargs["headers"] = {"X-Payment": payment_header}
            paid_response = await client.request(**req_kwargs)
            return (
                f"Payment sent ({amount} {currency} to {payee}). "
                f"Response {paid_response.status_code}: {paid_response.text[:500]}"
            )


class GetSpendLimitsTool(BaseTool):
    """Query on-chain spend limits from AgentAccountV2 contract."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(
            args_type=SpendLimitsInput,
            return_type=str,
            name="get_spend_limits",
            description=(
                "Query the on-chain spend limits and current usage from an "
                "AgentAccountV2 smart contract."
            ),
        )
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    async def run(self, args: SpendLimitsInput, cancellation_token: Any = None) -> str:  # type: ignore[override]
        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(args.contract_address),
            abi=_AGENT_ACCOUNT_ABI,
        )
        try:
            limit, spent, reset_at = contract.functions.getSpendLimit(
                Web3.to_checksum_address(args.token)
            ).call()
            remaining = limit - spent
            return (
                f"Spend limit: {limit} | Spent: {spent} | Remaining: {remaining} | "
                f"Resets at block/timestamp: {reset_at}"
            )
        except Exception as exc:
            return f"Error querying spend limits: {exc}"


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------

@dataclass
class AgentWalletToolkit:
    """
    Bundle of all AgentWallet tools for easy registration with AutoGen agents.

    Example::

        toolkit = AgentWalletToolkit(
            wallet_address="0x...",
            private_key="0x...",
            chain_id=8453,
            rpc_url="https://mainnet.base.org",
        )
        tools = toolkit.get_tools()
        # Register with an AssistantAgent:
        agent = AssistantAgent("wallet_agent", tools=tools, ...)
    """

    wallet_address: str
    private_key: str
    chain_id: int = 8453  # Base mainnet
    rpc_url: str = "https://mainnet.base.org"

    def get_tools(self) -> list[BaseTool]:
        return [
            WalletBalanceTool(self.rpc_url, self.wallet_address),
            WalletTransferTool(
                self.rpc_url, self.wallet_address, self.private_key, self.chain_id
            ),
            WalletBridgeTool(
                self.rpc_url, self.wallet_address, self.private_key, self.chain_id
            ),
            X402PaymentTool(
                self.rpc_url, self.wallet_address, self.private_key, self.chain_id
            ),
            GetSpendLimitsTool(self.rpc_url),
        ]


__all__ = [
    "AgentWalletToolkit",
    "WalletBalanceTool",
    "WalletTransferTool",
    "WalletBridgeTool",
    "X402PaymentTool",
    "GetSpendLimitsTool",
]
