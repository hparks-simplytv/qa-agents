import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from qa_agents.agents import Anthropic
from qa_agents.cli import main
from qa_agents.demo import commit
from qa_agents.runner import deterministic_review, locked, run


class FakeAgent:
    identity = {"provider": "test", "model": "fixture"}

    def __init__(self, mutate=None):
        self.usage = []
        self.calls = []
        self.mutate = mutate

    def ask(self, role, context):
        self.calls.append(role)
        if role == "beacon":
            return {"checks": [], "gaps": []}
        review = deterministic_review(context["brief"], context["evidence"])
        if self.mutate:
            self.mutate(review)
        return review


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "value.txt").write_text("accepted\n")
        sha = commit(self.repo, "Test fixture")
        self.state = self.root / "state"
        self.request = {"version": 1, "task_id": "task", "run_id": "run-1",
            "brief_version": "1", "environment_id": "test", "repo": str(self.repo),
            "base": sha, "candidate": sha, "outcome": "Preserve accepted value",
            "criteria": [{"id": "value", "text": "Value equals accepted", "checks": ["value"]}]}
        self.profile = {"version": 1, "checks": {"value": {
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'accepted\\n'"],
            "required": True, "timeout_seconds": 5}}}

    def execute(self, agent=None):
        return run(self.request, self.profile, self.state, agent)

    def test_cli_termination_reaps_running_check(self):
        marker = self.root/'child-pid'
        self.profile['checks']['value']['argv'] = [sys.executable, '-c',
            'import os,time; from pathlib import Path; Path('+repr(str(marker))+').write_text(str(os.getpid())); time.sleep(60)']
        request, profile = self.root/'request.json', self.root/'profile.json'
        request.write_text(json.dumps(self.request)); profile.write_text(json.dumps(self.profile))
        process = subprocess.Popen([sys.executable, '-m', 'qa_agents', 'run', str(request), '--profile', str(profile),
                                    '--state-dir', str(self.state)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+5
            while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue(marker.exists())
            child = int(marker.read_text())
            process.terminate(); process.wait(timeout=3)
            with self.assertRaises(ProcessLookupError): os.kill(child, 0)
            self.assertFalse((self.state/'run-1/result.json').exists())
        finally:
            if process.poll() is None: process.kill(); process.wait()

    def test_pass_is_bound_to_commit_and_actual_logs(self):
        result = self.execute()
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["candidate"], self.request["candidate"])
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertIn("value.log", result["artifacts"])
        self.assertIn("candidate.tar", result["artifacts"])

    def test_dirty_checkout_is_not_the_tested_candidate(self):
        (self.repo / "value.txt").write_text("uncommitted\n")
        result = self.execute()
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual((self.repo / "value.txt").read_text(), "uncommitted\n")

    def test_missing_mapping_blocks_despite_passing_suite(self):
        self.request["criteria"][0]["checks"] = []
        result = self.execute()
        self.assertEqual(result["checks"][0]["status"], "passed")
        self.assertEqual(result["verdict"], "blocked")

    def test_failed_check_fails_without_claiming_product_cause(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c", "raise SystemExit(1)"]
        result = self.execute()
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("diagnose", result["next_action"])

    def test_failed_checks_remain_visible_when_other_evidence_missing(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c", "raise SystemExit(1)"]
        self.request["criteria"].append({"id": "unknown", "text": "Uncovered behavior", "checks": []})
        result = self.execute()
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["checks"][0]["status"], "failed")

    def test_missing_executable_blocks(self):
        self.profile["checks"]["value"]["argv"] = [str(self.root / "not-installed")]
        self.assertEqual(self.execute()["verdict"], "blocked")

    def test_timeout_kills_work_and_blocks(self):
        self.profile["checks"]["value"].update(
            argv=[sys.executable, "-c", "import time; time.sleep(20)"], timeout_seconds=1)
        result = self.execute()
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("timed out", result["checks"][0]["reason"])

    def test_log_limit_blocks_even_fast_successful_process(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c", "print('x' * 1100000)"]
        self.assertEqual(self.execute()["verdict"], "blocked")
        self.assertLessEqual((self.state / "run-1/value.log").stat().st_size, 1024 * 1024)

    def test_duplicate_run_returns_receipt_without_reexecution(self):
        result = self.execute()
        with patch("qa_agents.runner.collect", side_effect=AssertionError("duplicate execution")):
            self.assertEqual(self.execute(), result)

    def test_changed_brief_profile_or_revision_cannot_reuse_id(self):
        self.execute()
        for field in ("brief_version", "candidate"):
            original = self.request[field]
            self.request[field] = "2" if field == "brief_version" else "a" * 40
            with self.assertRaisesRegex(ValueError, "different inputs"):
                self.execute()
            self.request[field] = original
        self.profile["checks"]["value"]["timeout_seconds"] = 4
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.execute()

    def test_missing_or_changed_evidence_rejects_cached_pass(self):
        self.execute()
        log = self.state / "run-1/value.log"
        log.write_text("altered")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.execute()
        log.unlink()
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.execute()

    def test_interrupted_run_is_not_relaunched(self):
        with patch("qa_agents.runner.collect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute()
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.execute()

    def test_second_run_cannot_enter_active_state_directory(self):
        with locked(self.state):
            with self.assertRaisesRegex(ValueError, "active"):
                self.execute()

    def test_each_check_gets_fresh_source(self):
        self.profile["checks"]["first"] = {"argv": [sys.executable, "-c",
            "from pathlib import Path; Path('value.txt').write_text('mutated')"],
            "required": True, "timeout_seconds": 5}
        self.assertEqual(self.execute()["verdict"], "pass")
        self.assertEqual((self.repo / "value.txt").read_text(), "accepted\n")

    def test_check_environment_does_not_inherit_api_credentials(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c",
            "import os; assert 'ANTHROPIC_API_KEY' not in os.environ; assert 'AWS_SECRET_ACCESS_KEY' not in os.environ"]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-secret", "AWS_SECRET_ACCESS_KEY": "test-secret"}):
            self.assertEqual(self.execute()["verdict"], "pass")

    def test_symlink_snapshot_blocks(self):
        (self.repo / "link").symlink_to("value.txt")
        self.request["candidate"] = commit(self.repo, "Link fixture")
        self.assertEqual(self.execute()["verdict"], "blocked")

    def test_unknown_check_and_path_traversal_rejected_before_execution(self):
        self.request["run_id"] = "../escape"
        with self.assertRaisesRegex(ValueError, "Invalid run_id"):
            self.execute()
        self.request["run_id"] = "valid"
        self.request["criteria"][0]["checks"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "unknown check"):
            self.execute()
        self.assertFalse(self.state.exists())

    def test_model_cannot_drop_required_checks(self):
        agent = FakeAgent()
        result = self.execute(agent)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(agent.calls, ["beacon", "inspector"])
        self.assertEqual(len(result["checks"]), 1)

    def test_model_cannot_claim_success_on_failed_evidence(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c", "raise SystemExit(1)"]
        agent = FakeAgent(lambda r: r["assessments"][0].update(status="supported"))
        result = self.execute(agent)
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("without successful", result["gaps"][0])

    def test_model_cannot_omit_criterion_or_invent_evidence(self):
        for index, mutation in enumerate([
            lambda r: r.update(assessments=[]),
            lambda r: r["assessments"][0].update(evidence=["invented"]),
        ]):
            self.request["run_id"] = f"case-{index}"
            self.assertEqual(self.execute(FakeAgent(mutation))["verdict"], "blocked")

    def test_provider_failure_becomes_blocked_result(self):
        agent = FakeAgent()
        agent.ask = lambda *args: (_ for _ in ()).throw(ValueError("Provider unavailable"))
        result = self.execute(agent)
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["gaps"], ["Provider unavailable"])

    def test_review_files_are_read_from_candidate_not_dirty_checkout(self):
        self.profile["review_files"] = ["value.txt"]
        (self.repo / "value.txt").write_text("uncommitted")
        agent = FakeAgent()
        original = agent.ask
        def ask(role, context):
            self.assertEqual(context["files"]["value.txt"], "accepted\n")
            return original(role, context)
        agent.ask = ask
        self.assertEqual(self.execute(agent)["verdict"], "pass")

    def test_missing_review_file_blocks(self):
        self.profile["review_files"] = ["missing.py"]
        self.assertEqual(self.execute()["verdict"], "blocked")

    def test_truncated_model_evidence_cannot_pass(self):
        self.profile["checks"]["value"]["argv"] = [sys.executable, "-c", "print('x' * 13000)"]
        result = self.execute(FakeAgent())
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("Logs exceed", result["gaps"][0])

    def test_cli_resolves_repo_relative_to_request_and_returns_verdict_exit(self):
        request = copy.deepcopy(self.request)
        request["repo"] = "repo"
        (self.root / "request.json").write_text(json.dumps(request))
        (self.root / "profile.json").write_text(json.dumps(self.profile))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = main(["run", str(self.root / "request.json"), "--profile",
                         str(self.root / "profile.json"), "--state-dir", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["verdict"], "pass")


class ProviderTests(unittest.TestCase):
    def test_structured_api_request_and_usage_without_live_network(self):
        response = {"stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 3},
                    "content": [{"type": "text", "text": '{"checks": [], "gaps": []}'}]}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only"}), patch(
                "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as urlopen:
            agent = Anthropic("test-model")
            self.assertEqual(agent.ask("beacon", {"brief": "test"}), {"checks": [], "gaps": []})
            body = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
            self.assertNotIn("tools", body)
            self.assertEqual(agent.usage[0]["input_tokens"], 4)

    def test_incomplete_model_response_is_not_accepted(self):
        response = {"stop_reason": "max_tokens", "content": []}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only"}), patch(
                "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(ValueError, "did not complete"):
                Anthropic("test-model").ask("inspector", {})


if __name__ == "__main__":
    unittest.main()
