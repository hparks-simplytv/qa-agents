import argparse
import json
from pathlib import Path
import sys

from .agents import Anthropic
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect evidence and review a Git candidate")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("run")
    check.add_argument("request", type=Path)
    check.add_argument("--profile", type=Path, required=True)
    check.add_argument("--state-dir", type=Path, default=Path(".qa-runs"))
    check.add_argument("--provider", choices=["deterministic", "anthropic"], default="deterministic")
    check.add_argument("--model", help="Required for Anthropic; choose a model available to your account")
    demo = sub.add_parser("demo", help="Run real checks against a synthetic pricing fixture")
    demo.add_argument("--state-dir", type=Path, default=Path(".qa-runs"))
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import demonstrate
            return demonstrate(args.state_dir)
        request = json.loads(args.request.read_text())
        # File references are relative to their request, independent of launch cwd.
        if isinstance(request, dict) and isinstance(request.get("repo"), str):
            request["repo"] = str((args.request.resolve().parent / request["repo"]).resolve())
        profile = json.loads(args.profile.read_text())
        agent = Anthropic(args.model) if args.provider == "anthropic" else None
        result = run(request, profile, args.state_dir, agent)
        print(json.dumps(result, indent=2))
        return {"pass": 0, "fail": 1, "blocked": 2}[result["verdict"]]
    except (ValueError, OSError) as exc:
        print(f"qa-agents: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
