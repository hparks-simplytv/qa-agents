"""Bounded agent calls. Models interpret evidence; the runner owns the verdict."""

import json
import os
from pathlib import Path
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
