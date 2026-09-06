"""Offline tests for the standalone treasury indices deployer."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "indices.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TreasuryIndicesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script()

    def test_scaled_rounds_up(self):
        self.assertEqual(self.mod.scaled(100, 11_500), 115)
        self.assertEqual(self.mod.scaled(1, 11_500), 2)

    def test_signed_transaction_compatibility(self):
        modern = type("ModernSigned", (), {"raw_transaction": b"modern"})()
        legacy = type("LegacySigned", (), {"rawTransaction": b"legacy"})()
        self.assertEqual(self.mod.signed_raw_transaction(modern), b"modern")
        self.assertEqual(self.mod.signed_raw_transaction(legacy), b"legacy")

    def test_confirmation_accepts_only_y_or_yes(self):
        for value in ("y", "yes", "Y", "YES", " Yes "):
            self.assertTrue(self.mod.confirmation_accepted(value))
        for value in ("", "n", "no", "okay", "true"):
            self.assertFalse(self.mod.confirmation_accepted(value))

    def test_validate_uint(self):
        maximum = 2**96 - 1
        self.assertEqual(self.mod.validate_uint(maximum, 96, "nonce"), maximum)
        with self.assertRaises(self.mod.ToolError):
            self.mod.validate_uint(maximum + 1, 96, "nonce")

    def test_equal_weights_always_total_ten_thousand(self):
        self.assertEqual(self.mod.equal_weights_bps(2), [5000, 5000])
        self.assertEqual(self.mod.equal_weights_bps(3), [3334, 3333, 3333])
        for count in range(1, 101):
            self.assertEqual(sum(self.mod.equal_weights_bps(count)), 10_000)

    def test_parse_asset_selection(self):
        for value in ("1,3,5", "1 3 5", "1 and 3 and 5", "1 dan 3 dan 5"):
            self.assertEqual(self.mod.parse_asset_selection(value, 3, 5), [1, 3, 5])
        for value, count, size in (("1,1", 2, 5), ("1,6", 2, 5), ("1,3", 3, 5)):
            with self.assertRaises(self.mod.ToolError):
                self.mod.parse_asset_selection(value, count, size)

    def test_distribution_percent_parser(self):
        self.assertEqual(self.mod.parse_distribution_percent("100"), 10_000)
        self.assertEqual(self.mod.parse_distribution_percent("25%"), 2_500)
        self.assertEqual(self.mod.parse_distribution_percent("12.34"), 1_234)
        self.assertEqual(self.mod.parse_distribution_percent("0"), 0)
        for value in ("-1", "100.01", "12.345", "abc"):
            with self.assertRaises(self.mod.ToolError):
                self.mod.parse_distribution_percent(value)

    def test_epoch_presets_and_custom_durations(self):
        presets = {"1": 900, "2": 3_600, "3": 21_600, "4": 86_400}
        for choice, seconds in presets.items():
            self.assertEqual(self.mod.parse_epoch_choice(choice), seconds)
        for value, seconds in (("15m", 900), ("1h", 3_600), ("6h", 21_600),
                               ("24h", 86_400), ("7d", 604_800), ("90s", 90)):
            self.assertEqual(self.mod.parse_epoch_choice(value), seconds)
        self.assertEqual(self.mod.format_epoch_duration(900), "15m")
        self.assertEqual(self.mod.format_epoch_duration(3_600), "1h")
        self.assertEqual(self.mod.format_epoch_duration(21_600), "6h")
        self.assertEqual(self.mod.format_epoch_duration(86_400), "24h")
        for value in ("0", "1.5h", "later", "-15m"):
            with self.assertRaises(self.mod.ToolError):
                self.mod.parse_epoch_choice(value)

    def test_interactive_distribution_settings(self):
        raw = {"treasury": {"distribute_bps": 10_000, "epoch_length": 900}}
        answers = iter(["37.5", "3"])
        result = self.mod.interactive_distribution_settings(
            raw, input_fn=lambda _: next(answers), output_fn=lambda _: None
        )
        self.assertEqual(result["treasury"]["distribute_bps"], 3_750)
        self.assertEqual(result["treasury"]["epoch_length"], 21_600)
        self.assertEqual(raw["treasury"]["distribute_bps"], 10_000)

    def test_interactive_selection_builds_equal_subset(self):
        raw = {"treasury": {"asset_catalog": [
            {"label": "One", "address": "0x0000000000000000000000000000000000000001"},
            {"label": "Two", "address": "0x0000000000000000000000000000000000000002"},
            {"label": "Three", "address": "0x0000000000000000000000000000000000000003"},
        ]}}
        answers = iter(["2", "1,3"])
        result = self.mod.interactive_asset_selection(
            raw, input_fn=lambda _: next(answers), output_fn=lambda _: None
        )
        self.assertNotIn("assets", raw["treasury"])
        self.assertEqual(result["treasury"]["weights_bps"], [5000, 5000])
        self.assertEqual(result["treasury"]["selected_asset_labels"], ["One", "Three"])

    def test_execute_requires_broadcast_before_rpc(self):
        result = subprocess.run([
            sys.executable,
            str(ROOT / "indices.py"),
            "execute",
            "--config",
            str(ROOT / "treasury.config.example.json"),
            "--rpc-url",
            "http://127.0.0.1:1",
        ], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("without --broadcast", result.stderr)
        self.assertNotIn("Unable to connect", result.stderr)

    def test_no_args_cancels_without_broadcast(self):
        with (
            patch.object(self.mod, "load_environment"),
            patch.object(self.mod, "load_json", return_value={}),
            patch.object(self.mod, "interactive_asset_selection", return_value={}),
            patch.object(self.mod, "interactive_distribution_settings", return_value={}),
            patch.object(self.mod, "make_web3", return_value=sentinel.web3),
            patch.object(
                self.mod,
                "resolve_launcher",
                return_value=("0x0000000000000000000000000000000000000001", "key"),
            ),
            patch.object(self.mod, "prepare", return_value=sentinel.plan),
            patch.object(self.mod, "public_plan", return_value={"mode": "preflight"}),
            patch.object(self.mod, "execute") as execute,
            patch("builtins.input", return_value="NO"),
        ):
            self.assertEqual(self.mod.main([]), 0)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
