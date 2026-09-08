"""Local trusted-code runner with immutable inputs and fail-closed verdicts."""

from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import tempfile
import time

from . import __version__

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
MAX_LOG = 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as out:
        json.dump(data, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def validate(request, profile):
    require(isinstance(request, dict) and isinstance(profile, dict), "Inputs must be objects")
    require(request.get("version") == 1 and profile.get("version") == 1, "Expected version 1")
    files = profile.get("review_files", [])
    require(strings(files) and len(files) <= 20 and all(
        not Path(f).is_absolute() and ".." not in Path(f).parts for f in files),
        "review_files must contain at most 20 repository-relative paths")
    for key in ("task_id", "run_id", "brief_version", "environment_id", "repo", "outcome"):
        require(isinstance(request.get(key), str) and request[key].strip(), f"Missing {key}")
    require(bool(ID.fullmatch(request["run_id"])), "Invalid run_id")
    for key in ("base", "candidate"):
        require(isinstance(request.get(key), str) and bool(SHA.fullmatch(request[key])),
                f"{key} must be a full immutable Git commit ID")
    checks = profile.get("checks")
    require(isinstance(checks, dict) and 0 < len(checks) <= 20, "Profile needs 1–20 checks")
    for name, check in checks.items():
        require(bool(ID.fullmatch(name)) and isinstance(check, dict), "Invalid check definition")
        require(strings(check.get("argv")) and check["argv"], f"{name}: argv must be a nonempty array")
        require(type(check.get("required")) is bool, f"{name}: required must be boolean")
        timeout = check.get("timeout_seconds")
        require(type(timeout) is int and 1 <= timeout <= 600, f"{name}: timeout must be 1–600 seconds")
    criteria = request.get("criteria")
    require(isinstance(criteria, list) and 0 < len(criteria) <= 50, "Need 1–50 criteria")
    seen = set()
    for criterion in criteria:
        require(isinstance(criterion, dict), "Criterion must be an object")
        cid = criterion.get("id")
        require(isinstance(cid, str) and bool(ID.fullmatch(cid)) and cid not in seen,
                "Criterion IDs must be valid and unique")
        seen.add(cid)
        require(isinstance(criterion.get("text"), str) and criterion["text"].strip(), "Criterion needs text")
        require(strings(criterion.get("checks")), "Criterion checks must be an array")
        require(set(criterion["checks"]) <= checks.keys(), "Criterion refers to an unknown check")


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    require(result.returncode == 0, "Git could not read the requested repository/revision")
    return result.stdout


def snapshot(request):
    repo = Path(request["repo"]).resolve()
    for key in ("base", "candidate"):
        actual = git(repo, "rev-parse", "--verify", request[key] + "^{commit}").decode().strip()
        require(actual == request[key], f"{key} must identify a commit directly")
    tree = git(repo, "ls-tree", "-r", request["candidate"])
    require(not any(line.startswith(b"160000 ") for line in tree.splitlines()),
            "Submodules need a materialization profile; snapshot blocked")
    archive = git(repo, "archive", "--format=tar", request["candidate"])
    diff = git(repo, "diff", "--no-ext-diff", "--no-textconv", request["base"], request["candidate"], "--")
    return archive, diff


def extract(archive, destination):
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        require(all(member.isfile() or member.isdir() for member in source.getmembers()),
                "Snapshot contains links or special files; a dedicated profile is needed")
        source.extractall(destination, filter="data")


def review_files(archive, names):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for name in names:
            try:
                member = source.getmember(name)
            except KeyError:
                raise ValueError(f"Review file missing from candidate: {name}") from None
            require(member.isfile() and member.size <= 30000, f"Review file is not a small regular file: {name}")
            files[name] = source.extractfile(member).read().decode(errors="replace")
    return files


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another QA run is active in this state directory") from None
        yield


def collect(name, check, archive, run_dir):
    log_path = run_dir / f"{name}.log"
    started = time.monotonic()
    status, code, reason = "blocked", None, ""
    with tempfile.TemporaryDirectory(prefix="qa-check-") as temp:
        work = Path(temp) / "work"
        work.mkdir()
        extract(archive, work)
        home = Path(temp) / "home"
        home.mkdir()
        # Avoid passing model credentials or the user's ambient application config.
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
               "TMPDIR": temp, "LANG": "C.UTF-8", "CI": "true",
               "PYTHONDONTWRITEBYTECODE": "1"}
        with log_path.open("wb") as log:
            process = None
            try:
                process = subprocess.Popen(check["argv"], cwd=work, env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                while process.poll() is None:
                    if time.monotonic() - started >= check["timeout_seconds"]:
                        reason = "Check timed out"
                        break
                    if log_path.stat().st_size > MAX_LOG:
                        reason = "Check exceeded log limit"
                        break
                    time.sleep(0.05)
                if not reason:
                    code = process.returncode
                    status = "passed" if code == 0 else "failed"
            except OSError:
                reason = "Check executable could not be started"
            finally:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
        if log_path.stat().st_size > MAX_LOG:
            status, reason = "blocked", "Check exceeded log limit"
            with log_path.open("r+b") as log:
                log.truncate(MAX_LOG)
    return {"name": name, "status": status, "exit_code": code, "reason": reason,
            "argv": check["argv"], "duration_seconds": round(time.monotonic() - started, 3),
            "log": log_path.name}


def default_plan(request, profile):
    checks = {name for name, check in profile["checks"].items() if check["required"]}
    gaps = []
    for criterion in request["criteria"]:
        checks.update(criterion["checks"])
        if not criterion["checks"]:
            gaps.append(f"{criterion['id']}: no verification check mapped to accepted behavior")
    return {"checks": sorted(checks), "gaps": gaps}


def validate_plan(plan, profile):
    require(isinstance(plan, dict) and strings(plan.get("checks")) and strings(plan.get("gaps")),
            "Beacon returned an invalid plan")
    require(set(plan["checks"]) <= profile["checks"].keys(), "Beacon selected an unknown check")


def deterministic_review(request, evidence):
    assessments = []
    by_name = {e["name"]: e for e in evidence}
    for criterion in request["criteria"]:
        checks = criterion["checks"]
        states = [by_name[name]["status"] for name in checks]
        status = "unverified"
        if states and all(s == "passed" for s in states):
            status = "supported"
        elif "failed" in states:
            status = "failed"
        assessments.append({"criterion_id": criterion["id"], "status": status,
            "evidence": checks,
            "reason": "Based on the operator's check mapping; semantic relevance was not model-reviewed"})
    return {"assessments": assessments, "gaps": []}


def validate_review(review, request, evidence):
    require(isinstance(review, dict) and isinstance(review.get("assessments"), list)
            and strings(review.get("gaps")), "Inspector returned an invalid review")
    ids = {c["id"] for c in request["criteria"]}
    seen = set()
    by_name = {e["name"]: e for e in evidence}
    mappings = {c["id"]: set(c["checks"]) for c in request["criteria"]}
    for assessment in review["assessments"]:
        require(isinstance(assessment, dict), "Invalid criterion assessment")
        cid = assessment.get("criterion_id")
        require(isinstance(cid, str) and cid in ids and cid not in seen, "Unknown/duplicate criterion assessment")
        seen.add(cid)
        status, refs = assessment.get("status"), assessment.get("evidence")
        require(status in ("supported", "failed", "unverified"), "Invalid assessment status")
        require(strings(refs) and set(refs) <= by_name.keys(), "Inspector cited unknown evidence")
        require(isinstance(assessment.get("reason"), str) and assessment["reason"].strip(),
                "Assessment needs a reason")
        if status == "supported":
            require(refs and mappings[cid] and mappings[cid] <= set(refs)
                    and all(by_name[name]["status"] == "passed" for name in refs),
                    "Inspector claimed support without successful mapped evidence")
        if status == "failed":
            require(refs and any(by_name[name]["status"] == "failed" for name in refs),
                    "Inspector claimed failure without failed check evidence")
    require(seen == ids, "Inspector omitted an acceptance criterion")


def verdict(plan, review, evidence):
    gaps = list(dict.fromkeys(plan["gaps"] + review["gaps"]))
    gaps.extend(f"{e['name']}: {e['reason']}" for e in evidence if e["status"] == "blocked")
    gaps.extend(a["criterion_id"] + ": " + a["reason"] for a in review["assessments"]
                if a["status"] == "unverified")
    if gaps:
        return "blocked", gaps
    if any(e["status"] == "failed" for e in evidence) or any(
            a["status"] == "failed" for a in review["assessments"]):
        return "fail", []
    return "pass", []


def verify_artifacts(run_dir, result):
    for name, expected in result["artifacts"].items():
        path = run_dir / name
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != expected:
            return False
    return True


def run(request, profile, state_dir, agent=None):
    validate(request, profile)
    # Resolve relative repository paths before persisting identity.
    request = {**request, "repo": str(Path(request["repo"]).resolve())}
    identity = agent.identity if agent else {"provider": "deterministic", "model": None}
    prompts = {p.name: digest(p.read_bytes()) for p in (Path(__file__).parent / "prompts").glob("*.md")}
    inputs = {"request": request, "profile": profile, "agent": identity,
              "runner_version": __version__, "prompts": prompts}
    fingerprint = digest(json.dumps(inputs, sort_keys=True).encode())
    root = Path(state_dir).resolve()
    with locked(root):
        run_dir = root / request["run_id"]
        if run_dir.exists():
            require((run_dir / "input.json").is_file(), "Incomplete run receipt; use a new run ID after inspection")
            previous = json.loads((run_dir / "input.json").read_text())
            require(previous["fingerprint"] == fingerprint, "Run ID already belongs to different inputs")
            if (run_dir / "result.json").exists():
                result = json.loads((run_dir / "result.json").read_text())
                require(verify_artifacts(run_dir, result), "Recorded evidence missing or changed; use a new run ID")
                return result
            raise ValueError("Run interrupted before a result was saved; inspect artifacts and use a new run ID")
        run_dir.mkdir()
        save(run_dir / "input.json", {"fingerprint": fingerprint, **inputs})
        result = {"version": 1, "task_id": request["task_id"], "run_id": request["run_id"],
                  "brief_version": request["brief_version"], "base": request["base"],
                  "candidate": request["candidate"], "environment_id": request["environment_id"],
                  "agent": identity, "verdict": "blocked", "assessments": [], "checks": [],
                  "gaps": [], "artifacts": {}, "artifact_dir": str(run_dir)}
        try:
            archive, diff = snapshot(request)
            (run_dir / "candidate.tar").write_bytes(archive)
            (run_dir / "change.diff").write_bytes(diff)
            plan = default_plan(request, profile)
            context = {"brief": request, "profile": profile,
                       "files": review_files(archive, profile.get("review_files", [])),
                       "diff": diff[:60000].decode(errors="replace"),
                       "diff_truncated": len(diff) > 60000}
            if agent:
                proposed = agent.ask("beacon", context)
                validate_plan(proposed, profile)
                plan = {"checks": sorted(set(plan["checks"] + proposed["checks"])),
                        "gaps": plan["gaps"] + proposed["gaps"]}
                if context["diff_truncated"]:
                    plan["gaps"].append("Diff exceeds model review limit; split the change")
            save(run_dir / "beacon.json", plan)
            for name in plan["checks"]:
                result["checks"].append(collect(name, profile["checks"][name], archive, run_dir))
                save(run_dir / "checks.json", result["checks"])
            review = deterministic_review(request, result["checks"])
            if agent:
                logs = {e["name"]: (run_dir / e["log"]).read_text(errors="replace")[:12000]
                        for e in result["checks"]}
                truncated = any((run_dir / e["log"]).stat().st_size > 12000 for e in result["checks"])
                if truncated:
                    plan["gaps"].append("Logs exceed model review limit; narrow checks or review full artifacts")
                    save(run_dir / "beacon.json", plan)
                review = agent.ask("inspector", {**context, "plan": plan,
                    "evidence": result["checks"], "log_excerpts": logs,
                    "logs_truncated": truncated})
            validate_review(review, request, result["checks"])
            save(run_dir / "inspector.json", review)
            result["assessments"] = review["assessments"]
            result["verdict"], result["gaps"] = verdict(plan, review, result["checks"])
        except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            result["gaps"].append(str(exc))
        result["usage"] = agent.usage if agent else []
        result["investigation_outcome"] = "blocked" if result["verdict"] == "blocked" else "acted"
        result["next_action"] = {"pass": "Review the scoped evidence package",
            "fail": "Return failed checks to engineering; diagnose and verify a new candidate",
            "blocked": "Resolve evidence gaps and submit a new run ID"}[result["verdict"]]
        result["artifacts"] = {p.name: digest(p.read_bytes()) for p in run_dir.iterdir() if p.is_file()}
        save(run_dir / "result.json", result)
        report = [f"# QA result: {result['verdict']}", "", f"Candidate: `{request['candidate']}`",
                  f"Mode: {identity['provider']}", "", result["next_action"], ""]
        report.extend(f"- {e['name']}: {e['status']} ({e['log']})" for e in result["checks"])
        report.extend(f"- Gap: {gap}" for gap in result["gaps"])
        (run_dir / "report.md").write_text("\n".join(report) + "\n")
        return result
