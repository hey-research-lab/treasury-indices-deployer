#!/usr/bin/env python3
"""Standalone configurable Index treasury deployer for Robinhood Chain."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from eth_abi import encode
from dotenv import load_dotenv
from web3 import Web3
from web3.logs import DISCARD

DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
DEFAULT_CHAIN_ID = 4663
DEFAULT_FACTORY = "0x05eAd7A0b471F640af176bA8EE91f1273EbAe204"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

TREASURY_FACTORY_ABI = [
    {
        "type": "function",
        "name": "deployTreasury",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "cfg",
                "type": "tuple",
                "components": [
                    {"name": "creator", "type": "address"},
                    {"name": "numeraire", "type": "address"},
                    {"name": "nonce", "type": "uint96"},
                    {"name": "distributeBps", "type": "uint16"},
                    {"name": "epochLength", "type": "uint32"},
                    {"name": "leafDustFloor", "type": "uint96"},
                    {"name": "basketHash", "type": "bytes32"},
                ],
            },
            {"name": "basketData", "type": "bytes"},
        ],
        "outputs": [{"name": "treasury", "type": "address"}],
    },
    {
        "type": "event",
        "name": "TreasuryDeployed",
        "anonymous": False,
        "inputs": [
            {"name": "treasury", "type": "address", "indexed": True},
            {"name": "core", "type": "address", "indexed": True},
            {"name": "creator", "type": "address", "indexed": True},
            {"name": "numeraire", "type": "address", "indexed": False},
            {"name": "salt", "type": "bytes32", "indexed": False},
        ],
    },
]


class ToolError(RuntimeError):
    """Raised for validation, RPC, simulation, and execution failures."""


def emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def confirmation_accepted(value: str) -> bool:
    """Accept y/yes confirmation in any letter case."""
    return value.strip().lower() in {"y", "yes"}


def signed_raw_transaction(signed: Any) -> bytes:
    """Return signed bytes across eth-account/Web3 v6 and v7 APIs."""
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raise ToolError("Signed transaction object does not expose raw transaction bytes.")
    return raw


def scaled(value: int, bps: int) -> int:
    return (value * bps + 9_999) // 10_000


def load_environment() -> None:
    script_env = Path(__file__).resolve().parent / ".env"
    cwd_env = Path.cwd() / ".env"
    load_dotenv(script_env, override=False)
    if cwd_env != script_env:
        load_dotenv(cwd_env, override=False)


def load_json(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Unable to read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError("Configuration root must be a JSON object.")
    return data


def make_web3(rpc_url: str, expected_chain_id: int) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ToolError("Unable to connect to the configured RPC URL.")
    actual_chain_id = int(w3.eth.chain_id)
    if actual_chain_id != expected_chain_id:
        raise ToolError(
            f"Wrong chain ID {actual_chain_id}; expected {expected_chain_id}."
        )
    return w3


def resolve_launcher(w3: Web3, cli_launcher: str | None, require_key: bool) -> tuple[str, str | None]:
    private_key = os.environ.get("PRIVATE_KEY")
    if private_key:
        try:
            address = w3.eth.account.from_key(private_key).address
        except Exception as exc:
            raise ToolError("PRIVATE_KEY is invalid.") from exc
        return Web3.to_checksum_address(address), private_key
    if require_key:
        raise ToolError("PRIVATE_KEY is required for execute.")
    candidate = cli_launcher or os.environ.get("LAUNCHER_ADDRESS")
    if not candidate:
        raise ToolError(
            "Set PRIVATE_KEY, LAUNCHER_ADDRESS, or pass --launcher for preflight."
        )
    return Web3.to_checksum_address(candidate), None


def validate_uint(value: Any, bits: int, label: str) -> int:
    number = int(value)
    if number < 0 or number >= 2**bits:
        raise ToolError(f"{label} must fit uint{bits}.")
    return number


def equal_weights_bps(asset_count: int) -> list[int]:
    """Split 10,000 bps as evenly as possible in deterministic order."""
    if asset_count <= 0 or asset_count > 10_000:
        raise ToolError("Asset count must be between 1 and 10,000.")
    base, remainder = divmod(10_000, asset_count)
    return [base + (1 if index < remainder else 0) for index in range(asset_count)]


def parse_distribution_percent(value: str) -> int:
    """Convert a holder distribution percentage into exact basis points."""
    text = value.strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        percent = Decimal(text)
    except InvalidOperation as exc:
        raise ToolError("Distribution percentage must be a number from 0 to 100.") from exc
    if not percent.is_finite() or percent < 0 or percent > 100:
        raise ToolError("Distribution percentage must be between 0 and 100.")
    basis_points = percent * 100
    if basis_points != basis_points.to_integral_value():
        raise ToolError("Distribution percentage supports at most two decimal places.")
    return int(basis_points)


def parse_epoch_duration(value: str) -> int:
    """Parse seconds or a compact duration such as 15m, 1h, or 1d."""
    text = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"([1-9]\d*)([smhdw]?)", text)
    if not match:
        raise ToolError(
            "Epoch must be a positive duration such as 15m, 1h, 1d, 7d, or 3600s."
        )
    amount = int(match.group(1))
    multiplier = {
        "": 1,
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }[match.group(2)]
    return validate_uint(amount * multiplier, 32, "Epoch duration")


def format_epoch_duration(seconds: int) -> str:
    """Format epoch seconds while preserving the four advertised preset labels."""
    preferred = {900: "15m", 3_600: "1h", 21_600: "6h", 86_400: "24h"}
    if seconds in preferred:
        return preferred[seconds]
    for suffix, unit in (("w", 604_800), ("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds % unit == 0:
            return f"{seconds // unit}{suffix}"
    return f"{seconds}s"


def parse_epoch_choice(value: str) -> int:
    """Resolve numbered presets while retaining compact custom durations."""
    presets = {"1": "15m", "2": "1h", "3": "6h", "4": "24h"}
    return parse_epoch_duration(presets.get(value.strip(), value))


def interactive_distribution_settings(
    raw: dict[str, Any], input_fn: Any = None, output_fn: Any = print
) -> dict[str, Any]:
    """Prompt for holder distribution percentage and epoch duration."""
    if input_fn is None:
        input_fn = input
    treasury = raw.get("treasury")
    if not isinstance(treasury, dict):
        raise ToolError("Missing treasury configuration object.")
    current_bps = validate_uint(treasury.get("distribute_bps", 10_000), 16, "distribute_bps")
    if current_bps > 10_000:
        raise ToolError("distribute_bps cannot exceed 10,000 (100%).")
    current_epoch = validate_uint(treasury.get("epoch_length", 900), 32, "epoch_length")
    if current_epoch == 0:
        raise ToolError("epoch_length must be greater than zero.")
    percent_default = f"{Decimal(current_bps) / 100:g}"
    epoch_default = format_epoch_duration(current_epoch)
    try:
        percent_text = input_fn(f"Distribute to holders (%) [{percent_default}]: ")
        output_fn("Distribution epoch presets:")
        output_fn("  1. 15m")
        output_fn("  2. 1h")
        output_fn("  3. 6h")
        output_fn("  4. 24h")
        epoch_text = input_fn(
            f"Select 1-4 or enter a custom duration [{epoch_default}]: "
        )
    except EOFError as exc:
        raise ToolError("Distribution settings cancelled because input was closed.") from exc
    distribute_bps = (
        current_bps if not str(percent_text).strip()
        else parse_distribution_percent(str(percent_text))
    )
    epoch_length = (
        current_epoch if not str(epoch_text).strip()
        else parse_epoch_choice(str(epoch_text))
    )
    updated = copy.deepcopy(raw)
    updated["treasury"]["distribute_bps"] = distribute_bps
    updated["treasury"]["epoch_length"] = epoch_length
    output_fn("\nDistribution settings:")
    output_fn(f"  To holders — {distribute_bps / 100:.2f}%")
    output_fn(f"  Every — {format_epoch_duration(epoch_length)} ({epoch_length} seconds)\n")
    return updated


def parse_asset_selection(value: str, expected_count: int, catalog_size: int) -> list[int]:
    """Parse selections such as '1,3', '1 3', or '1 dan 3'."""
    normalized = re.sub(r"\b(?:and|dan)\b", " ", value.strip(), flags=re.IGNORECASE)
    parts = [part for part in re.split(r"[\s,]+", normalized) if part]
    if not parts or any(not part.isdigit() for part in parts):
        raise ToolError("Asset selection must contain numbers separated by spaces or commas.")
    selections = [int(part) for part in parts]
    if len(selections) != expected_count:
        raise ToolError(
            f"Select exactly {expected_count} assets; received {len(selections)}."
        )
    if len(set(selections)) != len(selections):
        raise ToolError("The same asset number cannot be selected more than once.")
    if any(number < 1 or number > catalog_size for number in selections):
        raise ToolError(f"Asset numbers must be between 1 and {catalog_size}.")
    return selections


def interactive_asset_selection(
    raw: dict[str, Any], input_fn: Any = None, output_fn: Any = print
) -> dict[str, Any]:
    """Prompt for a subset of the configured asset catalog and equalize weights."""
    if input_fn is None:
        input_fn = input
    treasury = raw.get("treasury")
    if not isinstance(treasury, dict):
        raise ToolError("Missing treasury configuration object.")
    catalog = treasury.get("asset_catalog")
    if not isinstance(catalog, list) or not catalog:
        raise ToolError(
            "Interactive mode requires a non-empty treasury.asset_catalog in the config."
        )

    resolved: list[tuple[str, str]] = []
    for number, item in enumerate(catalog, 1):
        if not isinstance(item, dict):
            raise ToolError(f"asset_catalog entry {number} must be an object.")
        label = str(item.get("label") or f"Asset {number}").strip()
        try:
            address = Web3.to_checksum_address(item.get("address", ""))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"asset_catalog entry {number} has an invalid address.") from exc
        if address == Web3.to_checksum_address(ZERO_ADDRESS):
            raise ToolError(f"asset_catalog entry {number} cannot use the zero address.")
        resolved.append((label, address))
    addresses = [address for _, address in resolved]
    if len(set(addresses)) != len(addresses):
        raise ToolError("asset_catalog contains duplicate addresses.")

    output_fn("\nAvailable treasury assets:")
    for number, (label, address) in enumerate(resolved, 1):
        output_fn(f"  {number}. {label} — {address}")
    try:
        count_text = input_fn(f"How many assets do you want to use (1-{len(resolved)})? ")
        if not str(count_text).strip().isdigit():
            raise ToolError("Asset count must be a number.")
        count = int(str(count_text).strip())
        if count < 1 or count > len(resolved):
            raise ToolError(f"Asset count must be between 1 and {len(resolved)}.")
        selection_text = input_fn(
            f"Select exactly {count} asset numbers (example: 1,3,5): "
        )
    except EOFError as exc:
        raise ToolError("Asset selection cancelled because input was closed.") from exc

    selections = parse_asset_selection(str(selection_text), count, len(resolved))
    selected = [resolved[number - 1] for number in selections]
    weights = equal_weights_bps(count)
    updated = copy.deepcopy(raw)
    updated_treasury = updated["treasury"]
    updated_treasury["assets"] = [address for _, address in selected]
    updated_treasury["weights_bps"] = weights
    updated_treasury["selected_asset_labels"] = [label for label, _ in selected]

    output_fn("\nSelected basket:")
    for label, address, weight in zip(
        updated_treasury["selected_asset_labels"],
        updated_treasury["assets"],
        weights,
    ):
        output_fn(f"  - {label} — {address} — {weight / 100:.2f}%")
    output_fn("  Total — 100.00%\n")
    return updated


def prepare(w3: Web3, raw: dict[str, Any], launcher: str) -> dict[str, Any]:
    factory_address = Web3.to_checksum_address(
        raw.get("factory_address", DEFAULT_FACTORY)
    )
    if not w3.eth.get_code(factory_address):
        raise ToolError(f"No contract bytecode at treasury factory {factory_address}.")

    treasury = raw.get("treasury")
    if not isinstance(treasury, dict):
        raise ToolError("Missing treasury configuration object.")

    creator = Web3.to_checksum_address(treasury.get("creator") or launcher)
    assets = [Web3.to_checksum_address(item) for item in treasury.get("assets", [])]
    weights = [int(item) for item in treasury.get("weights_bps", [])]
    if not assets or len(assets) != len(weights):
        raise ToolError("assets and weights_bps must have the same nonzero length.")
    if len(set(assets)) != len(assets):
        raise ToolError("The selected basket contains duplicate asset addresses.")
    zero_address = Web3.to_checksum_address(ZERO_ADDRESS)
    for asset in assets:
        if asset == zero_address:
            raise ToolError("Basket assets cannot use the zero address.")
        if not w3.eth.get_code(asset):
            raise ToolError(f"No contract bytecode at basket asset {asset}.")
    if sum(weights) != 10_000:
        raise ToolError("weights_bps must sum to 10,000.")
    for weight in weights:
        validate_uint(weight, 16, "Each basket weight")
    labels = treasury.get("selected_asset_labels") or [
        f"Asset {index}" for index in range(1, len(assets) + 1)
    ]
    if not isinstance(labels, list) or len(labels) != len(assets):
        raise ToolError("selected_asset_labels must match the selected asset count.")

    configured_nonce = treasury.get("nonce")
    config_nonce = (
        secrets.randbits(96)
        if configured_nonce is None
        else validate_uint(configured_nonce, 96, "Treasury nonce")
    )
    basket_data = encode(["address[]", "uint16[]"], [assets, weights])
    basket_hash = Web3.keccak(basket_data)
    distribute_bps = validate_uint(
        treasury.get("distribute_bps", 10_000), 16, "distribute_bps"
    )
    if distribute_bps > 10_000:
        raise ToolError("distribute_bps cannot exceed 10,000 (100%).")
    epoch_length = validate_uint(treasury.get("epoch_length", 900), 32, "epoch_length")
    if epoch_length == 0:
        raise ToolError("epoch_length must be greater than zero.")
    cfg_tuple = (
        creator,
        Web3.to_checksum_address(treasury.get("numeraire", ZERO_ADDRESS)),
        config_nonce,
        distribute_bps,
        epoch_length,
        validate_uint(treasury.get("leaf_dust_floor", 0), 96, "leaf_dust_floor"),
        basket_hash,
    )

    contract = w3.eth.contract(address=factory_address, abi=TREASURY_FACTORY_ABI)
    function = contract.functions.deployTreasury(cfg_tuple, basket_data)
    options = {"from": launcher}
    try:
        predicted = Web3.to_checksum_address(function.call(options))
        gas_estimate = int(function.estimate_gas(options))
    except Exception as exc:
        raise ToolError(f"Treasury preflight failed: {exc}") from exc

    return {
        "factory_address": factory_address,
        "creator": creator,
        "assets": assets,
        "weights_bps": weights,
        "asset_labels": [str(label) for label in labels],
        "config_nonce": config_nonce,
        "distribute_bps": distribute_bps,
        "epoch_length": epoch_length,
        "basket_data": basket_data,
        "basket_hash": basket_hash,
        "cfg_tuple": cfg_tuple,
        "predicted_treasury": predicted,
        "gas_estimate": gas_estimate,
        "contract": contract,
        "function": function,
    }


def fee_fields(w3: Web3, raw: dict[str, Any]) -> dict[str, int]:
    max_fee_bps = int(raw.get("max_fee_multiplier_bps", 15_000))
    priority = int(raw.get("max_priority_fee_per_gas_wei", 0))
    if max_fee_bps < 10_000 or priority < 0:
        raise ToolError("Invalid max-fee multiplier or priority fee.")
    pending = w3.eth.get_block("pending")
    base_fee = int(pending.get("baseFeePerGas") or w3.eth.gas_price)
    return {
        "maxPriorityFeePerGas": priority,
        "maxFeePerGas": scaled(base_fee, max_fee_bps) + priority,
    }


def public_plan(w3: Web3, raw: dict[str, Any], launcher: str, plan: dict[str, Any]) -> dict[str, Any]:
    gas_bps = int(raw.get("gas_limit_multiplier_bps", 11_500))
    if gas_bps < 10_000:
        raise ToolError("gas_limit_multiplier_bps must be at least 10,000.")
    fees = fee_fields(w3, raw)
    gas_limit = scaled(plan["gas_estimate"], gas_bps)
    return {
        "mode": "preflight",
        "chain_id": int(w3.eth.chain_id),
        "launcher": launcher,
        "factory": plan["factory_address"],
        "creator": plan["creator"],
        "predicted_treasury": plan["predicted_treasury"],
        "treasury_config_nonce": plan["config_nonce"],
        "distribute_to_holders_bps": plan["distribute_bps"],
        "distribute_to_holders_percent": f"{plan['distribute_bps'] / 100:.2f}%",
        "epoch_length_seconds": plan["epoch_length"],
        "epoch": format_epoch_duration(plan["epoch_length"]),
        "basket_hash": Web3.to_hex(plan["basket_hash"]),
        "basket": [
            {
                "label": label,
                "address": address,
                "weight_bps": weight,
                "percent": f"{weight / 100:.2f}%",
            }
            for label, address, weight in zip(
                plan["asset_labels"], plan["assets"], plan["weights_bps"]
            )
        ],
        "gas_estimate": plan["gas_estimate"],
        "buffered_gas_limit": gas_limit,
        "max_fee_per_gas_wei": fees["maxFeePerGas"],
        "estimated_max_cost_wei": gas_limit * fees["maxFeePerGas"],
    }


def execute(w3: Web3, raw: dict[str, Any], launcher: str, private_key: str, plan: dict[str, Any]) -> dict[str, Any]:
    gas_bps = int(raw.get("gas_limit_multiplier_bps", 11_500))
    if gas_bps < 10_000:
        raise ToolError("gas_limit_multiplier_bps must be at least 10,000.")
    fees = fee_fields(w3, raw)
    gas_limit = scaled(plan["gas_estimate"], gas_bps)
    max_cost = gas_limit * fees["maxFeePerGas"]
    balance = int(w3.eth.get_balance(launcher))
    if balance < max_cost:
        raise ToolError(f"Insufficient balance; buffered ceiling is {max_cost} wei.")

    nonce = int(w3.eth.get_transaction_count(launcher, "pending"))
    tx = plan["function"].build_transaction(
        {
            "from": launcher,
            "chainId": int(w3.eth.chain_id),
            "nonce": nonce,
            "value": 0,
            "gas": gas_limit,
            "type": 2,
            **fees,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_raw_transaction(signed))
    tx_hash_text = tx_hash.hex()
    emit(f"[SUBMITTED] Treasury transaction: {tx_hash_text}")
    receipt = w3.eth.wait_for_transaction_receipt(
        tx_hash,
        timeout=int(raw.get("receipt_timeout_seconds", 180)),
        poll_latency=0.25,
    )
    if int(receipt.status) != 1:
        raise ToolError(f"Treasury transaction reverted: {tx_hash_text}")
    events = plan["contract"].events.TreasuryDeployed().process_receipt(
        receipt, errors=DISCARD
    )
    if len(events) != 1:
        raise ToolError(f"Expected one TreasuryDeployed event, found {len(events)}.")
    treasury = Web3.to_checksum_address(events[0]["args"]["treasury"])
    if treasury != plan["predicted_treasury"]:
        raise ToolError(
            f"Predicted treasury {plan['predicted_treasury']} does not match emitted {treasury}."
        )
    emit(f"[CONFIRMED] Treasury created: {treasury}")
    return {
        "mode": "execute",
        "chain_id": int(w3.eth.chain_id),
        "transaction_hash": tx_hash_text,
        "block_number": int(receipt.blockNumber),
        "gas_used": int(receipt.gasUsed),
        "launcher": launcher,
        "creator": plan["creator"],
        "treasury": treasury,
        "treasury_config_nonce": plan["config_nonce"],
        "distribute_to_holders_bps": plan["distribute_bps"],
        "distribute_to_holders_percent": f"{plan['distribute_bps'] / 100:.2f}%",
        "epoch_length_seconds": plan["epoch_length"],
        "epoch": format_epoch_duration(plan["epoch_length"]),
        "basket_hash": Web3.to_hex(plan["basket_hash"]),
    }


def write_result(output_dir: str, prefix: str, result: dict[str, Any]) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{prefix}-{timestamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path.resolve()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Standalone Index treasury deployer.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("preflight", "execute"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="treasury.config.json")
        command.add_argument("--rpc-url", default=os.environ.get("RPC_URL", DEFAULT_RPC_URL))
        command.add_argument("--launcher", help="Public launcher address for keyless preflight.")
    commands.choices["execute"].add_argument(
        "--broadcast", action="store_true", help="Required to broadcast the transaction."
    )
    commands.choices["execute"].add_argument("--output-dir", default="runs")
    return root


def main(argv: list[str] | None = None) -> int:
    load_environment()
    provided_args = list(sys.argv[1:] if argv is None else argv)
    interactive = not provided_args
    if interactive:
        args = argparse.Namespace(
            command="interactive",
            config="treasury.config.json",
            rpc_url=os.environ.get("RPC_URL", DEFAULT_RPC_URL),
            launcher=None,
            output_dir="runs",
        )
    else:
        args = parser().parse_args(provided_args)
    try:
        if args.command == "execute" and not args.broadcast:
            raise ToolError("Refusing live execution without --broadcast.")
        raw = load_json(args.config)
        if interactive:
            raw = interactive_asset_selection(raw)
            raw = interactive_distribution_settings(raw)
        expected_chain_id = int(raw.get("chain_id", DEFAULT_CHAIN_ID))
        w3 = make_web3(args.rpc_url, expected_chain_id)
        require_key = args.command in ("execute", "interactive")
        launcher, private_key = resolve_launcher(w3, args.launcher, require_key)
        plan = prepare(w3, raw, launcher)
        preview = public_plan(w3, raw, launcher, plan)
        if args.command == "preflight":
            print(json.dumps(preview, indent=2))
            return 0
        if interactive:
            print(json.dumps(preview, indent=2))
            print("\nPreflight passed. This will broadcast a live treasury deployment.")
            try:
                confirmation = input("Broadcast transaction? Type y or yes to continue: ")
            except EOFError:
                confirmation = ""
            if not confirmation_accepted(confirmation):
                print("Cancelled. No transaction was broadcast.")
                return 0
        assert private_key is not None
        result = execute(w3, raw, launcher, private_key, plan)
        result_path = write_result(args.output_dir, "treasury", result)
        print(json.dumps({**result, "result_file": str(result_path)}, indent=2))
        return 0
    except (ToolError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
