"""Synthetic pricing demonstration, distinct from the upstream static case study."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .runner import run

BUGGY = "def total(price, quantity, discount):\n    return price * quantity - price * discount\n"
FIXED = "def total(price, quantity, discount):\n    return price * quantity * (1 - discount)\n"
UNIT = """import unittest
from pricing import total

class PricingTest(unittest.TestCase):
    def test_single_item(self):
        self.assertEqual(total(100, 1, 0.1), 90)
"""
REGRESSION = """import unittest
from pricing import total

class DiscountTest(unittest.TestCase):
    def test_discount_applies_to_every_item(self):
        self.assertEqual(total(100, 3, 0.1), 270)
"""


def commit(repo, message):
    def execute(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()
    execute("add", ".")
    execute("-c", "user.name=QA Demo", "-c", "user.email=qa-demo@localhost",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", message)
    return execute("rev-parse", "HEAD")


def demonstrate(state_dir):
    prefix = "demo-" + uuid.uuid4().hex[:12]
    profile = {"version": 1, "checks": {
        "unit": {"argv": [sys.executable, "-m", "unittest", "test_pricing"],
                 "required": True, "timeout_seconds": 10},
        "regression": {"argv": [sys.executable, "-m", "unittest", "test_discount"],
                       "required": False, "timeout_seconds": 10}}}
    results = []
    with tempfile.TemporaryDirectory(prefix="qa-demo-") as temp:
        repo = Path(temp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "pricing.py").write_text(FIXED)
        (repo / "test_pricing.py").write_text(UNIT)
        base = commit(repo, "Synthetic baseline")
        (repo / "pricing.py").write_text(BUGGY)
        buggy = commit(repo, "Introduce quantity discount regression")
        request = {"version": 1, "task_id": prefix, "run_id": prefix + "-gap",
            "brief_version": "1", "environment_id": "synthetic-python-pricing",
            "repo": str(repo), "base": base, "candidate": buggy,
            "outcome": "Apply a 10 percent discount to all three 100-unit items",
            "criteria": [{"id": "quantity-discount", "text": "total(100, 3, 0.1) equals 270",
                          "checks": []}]}
        results.append(run(request, profile, state_dir))
        (repo / "test_discount.py").write_text(REGRESSION)
        request["candidate"] = commit(repo, "Add accepted regression example")
        request["criteria"][0]["checks"] = ["regression"]
        request["run_id"] = prefix + "-failure"
        results.append(run(request, profile, state_dir))
        (repo / "pricing.py").write_text(FIXED)
        request["candidate"] = commit(repo, "Fix discount calculation")
        request["run_id"] = prefix + "-pass"
        results.append(run(request, profile, state_dir))
    summary = [{"run_id": r["run_id"], "verdict": r["verdict"],
                "report": str(Path(r["artifact_dir"]) / "report.md")} for r in results]
    print(json.dumps(summary, indent=2))
    return 0 if [r["verdict"] for r in results] == ["blocked", "fail", "pass"] else 1
