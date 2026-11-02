"""Regression tests for the CLEAN forwarder's signal/update classifier.

Runs the REAL classifier functions from worker_clean_signal_forwarder.py
without importing the worker (which needs a Telegram session).

    python -m unittest discover -s tests -v
"""

import ast
import re
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "telegram_worker" / "worker_clean_signal_forwarder.py"


def load_classifier():
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    keep = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in ("is_my_formatted_signal", "is_clean_signal_update"))
        or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "HEADER_RE" for t in node.targets))
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(WORKER), "exec"), namespace)
    return namespace["is_my_formatted_signal"], namespace["is_clean_signal_update"]


is_signal, is_update = load_classifier()

# Exact text produced by the formatter (universal_signal_ai) for signal #87240
# on 1 Nov 2026, which CLEAN logged as "[SKIP UPDATE DISABLED]".
FORMATTED_SELL = """🔴SELL XAUUSD ZONE

• Sell Point : 3904
• Stop Loss : 3909

📌TP1 - 3899
📌TP2 - 3894
📌TP3 - 3889
📌TP4 - 3884
📌TP5 - Open

⚠️ RISK: HIGH

TIPS:
Breakeven after TP1 HIT ❗️
Use correct Risk management ❗️
Take spread into consideration when placing SL❗️
THIS IS NOT FINANCIAL ADVICE ⚠️"""

FORMATTED_BREAKOUT = """🚨SELL STOP XAUUSD BREAKOUT ZONE

• Trigger Below : 3890
• Sell Stop Entry : 3890
• Stop Loss : 3898

📌TP1 - 3880
📌TP2 - Open

⚠️ RISK: MEDIUM

TIPS:
Only enter if price breaks below 3890 ❗️
Breakeven after TP1 HIT ❗️
THIS IS NOT FINANCIAL ADVICE ⚠️"""


class CleanClassifierTests(unittest.TestCase):

    def test_formatted_signals_are_forwarded_not_skipped_as_updates(self):
        for text in (FORMATTED_SELL, FORMATTED_BREAKOUT):
            self.assertTrue(is_signal(text))
            self.assertFalse(is_update(text), "a complete signal must not be treated as an update")

    def test_real_updates_are_still_updates(self):
        for text in ("TP1 HIT +50 PIPS ✅", "TP2 hit", "SL to BE", "Breakeven hit", "+120 pips running"):
            self.assertTrue(is_update(text), text)
            self.assertFalse(is_signal(text), text)

    def test_incomplete_or_source_lines_are_neither(self):
        for text in ("💎 Source: Topic 26902 #87240", "🔴 XAUUSD SELL LIMITE\nENTRY: 3904\nSL: 3909", ""):
            self.assertFalse(is_signal(text), text)
        self.assertFalse(is_update("💎 Source: Topic 26902 #87240"))

    def test_signal_missing_disclaimer_is_not_forwarded(self):
        partial = FORMATTED_SELL.replace("THIS IS NOT FINANCIAL ADVICE ⚠️", "")
        self.assertFalse(is_signal(partial))


if __name__ == "__main__":
    unittest.main()
