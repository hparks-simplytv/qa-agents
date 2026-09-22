"""Bounded agent calls. Models interpret evidence; the runner owns the verdict."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


STRINGS = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = object_schema({"checks": STRINGS, "gaps": STRINGS})
REVIEW_SCHEMA = object_schema({
    "assessments": {"type": "array", "items": object_schema({
        "criterion_id": {"type": "string"},
        "status": {"type": "string", "enum": ["supported", "failed", "unverified"]},
        "evidence": STRINGS, "reason": {"type": "string"}})},
    "gaps": STRINGS,
})


class Anthropic:
    """Two bounded Messages API calls; no tools, retry, or provider fallback."""

    def __init__(self, model, timeout=60):
        if not model or not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("Claude mode requires --model and ANTHROPIC_API_KEY")
        self.model = model
        self.timeout = timeout
        self.usage = []

    @property
    def identity(self):
        return {"provider": "anthropic", "model": self.model}

    def ask(self, role, context):
        prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text()
        body = {"model": self.model, "max_tokens": 4096, "system": prompt,
                "messages": [{"role": "user", "content": json.dumps(context)}],
                "output_config": {"format": {"type": "json_schema",
                    "schema": PLAN_SCHEMA if role == "beacon" else REVIEW_SCHEMA}}}
        payload = json.dumps(body).encode()
        if len(payload) > 180000:
            raise ValueError("Agent context exceeds 180 KB; reduce the review scope")
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                     "x-api-key": os.environ["ANTHROPIC_API_KEY"]})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not persist response bodies, which can contain request content.
            raise ValueError(f"Claude API returned HTTP {exc.code}; no retry performed") from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("Claude API unavailable; no retry performed") from None
        if not isinstance(result, dict) or not isinstance(result.get("usage", {}), dict):
            raise ValueError("Claude API returned an invalid response")
        self.usage.append({"role": role, **result.get("usage", {})})
        if result.get("stop_reason") != "end_turn":
            raise ValueError(f"{role} did not complete a structured response")
        content = result.get("content")
        if not isinstance(content, list) or not all(isinstance(b, dict) for b in content):
            raise ValueError("Claude API returned invalid content")
        blocks = [b.get("text") for b in content if b.get("type") == "text"]
        if len(blocks) != 1 or not isinstance(blocks[0], str):
            raise ValueError(f"{role} returned an unexpected response")
        return json.loads(blocks[0])


class ClaudeSubscription:
    """Tool-free structured reviews using the authenticated Claude subscription."""

    def __init__(self, model, timeout=180):
        if not model:
            raise ValueError("Claude subscription QA requires --model")
        self.model, self.timeout, self.usage = model, timeout, []

    @property
    def identity(self):
        return {"provider": "claude-subscription", "model": self.model}

    def ask(self, role, context):
        prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text()
        payload = json.dumps(context)
        if len(payload.encode()) > 400000:
            raise ValueError("Agent context exceeds 400 KB; reduce the review scope")
        # Never inherit API keys, provider redirects, plugins, or paid fallbacks.
        env = {k: v for k, v in os.environ.items()
               if k in {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG"}}
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        schema = PLAN_SCHEMA if role == "beacon" else REVIEW_SCHEMA
        argv = ["claude", "--print", "--model", self.model, "--permission-mode", "dontAsk",
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
                "--tools", "", "--allowedTools", "StructuredOutput", "--output-format", "json",
                "--json-schema", json.dumps(schema), "--system-prompt", prompt]
        # No candidate checkout or repository instructions are exposed to the CLI.
        with tempfile.TemporaryDirectory(prefix="qa-subscription-") as work:
            try:
                auth = subprocess.run(["claude", "auth", "status"], env=env, cwd=work,
                                      capture_output=True, text=True, timeout=30)
                status = json.loads(auth.stdout)
                if (not isinstance(status, dict) or auth.returncode or status.get("loggedIn") is not True
                        or status.get("authMethod") != "claude.ai"):
                    raise ValueError("Claude subscription unavailable; no API fallback")
                process = subprocess.run(argv, input=payload, env=env, cwd=work,
                                         capture_output=True, text=True, timeout=self.timeout)
            except (subprocess.SubprocessError, OSError):
                raise ValueError("Claude subscription QA unavailable or timed out; no retry performed") from None
        try:
            response = json.loads(process.stdout)
        except ValueError:
            raise ValueError("Claude subscription QA returned no structured response") from None
        if (not isinstance(response, dict) or process.returncode or response.get("is_error")
                or response.get("subtype") != "success" or response.get("permission_denials")
                or not isinstance(response.get("structured_output"), dict)):
            raise ValueError("Claude subscription QA did not complete a structured review; no retry performed")
        self.usage.append({"role": role, "usage": response.get("usage", {}),
                           "modelUsage": response.get("modelUsage", {})})
        return response["structured_output"]
