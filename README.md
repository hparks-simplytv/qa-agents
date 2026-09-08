# QA Agents

A working first version of evidence-led QA for agent-built software. Beacon plans
verification, the runner executes named checks, and Inspector assesses the results
against accepted behavior. Runs return pass, fail, or blocked with logs and the
exact candidate revision.

This work fork extends [Haley's public QA reference](https://github.com/haleyparks329/qa-agents).
The [original case study](docs/upstream-case-study.md) and MIT attribution are
preserved. The runtime here is newly implemented.

## Run the demo

Requires Python 3.12+ and Git on macOS or Linux. No packages, credentials, or model
calls are needed for the default mode.

```sh
python3 -m qa_agents demo
```

The demo creates a temporary synthetic pricing repository and runs real tests:

1. Existing tests pass, but quantity/discount behavior has no mapped regression
   check: **blocked**.
2. A regression assertion is added and catches the bug: **fail**.
3. The calculation is fixed and the same assertion succeeds: **pass**.

Reports remain under `.qa-runs/`. Demo commits exist only in its temporary fixture
repository. It does not modify an application checkout. This fixture is inspired
by Little Bytes; it is separate from the upstream static demo.

## Review an application

Customize [request.json](examples/request.json) and [profile.json](examples/profile.json).
Supply full base and candidate commit IDs, accepted behavior, and checks that
actually exercise that behavior. The example files contain placeholders and
application-specific test names; edit them before running.

```sh
python3 -m qa_agents run examples/request.json \
  --profile examples/profile.json --state-dir .qa-runs
```

The repository path resolves relative to the request file. Check commands run at
the candidate root. Only committed candidate contents are tested; uncommitted
changes are excluded. Each check gets a fresh exported snapshot, temporary home
and minimal environment. Dependencies must already be available to the configured
executable. Use an absolute virtual-environment interpreter path when needed.

The profile is trusted operator configuration: command argument arrays, timeouts,
mandatory checks and optional review_files containing relevant tests or application
code. Models can select existing checks but cannot supply commands or waive
required checks. Every criterion needs a mapped check; an empty mapping is an
explicit evidence gap.

Exit codes: **0** pass, **1** failed verification, **2** blocked or invalid input.

## Enable model-assisted Beacon and Inspector

The default deterministic mode evaluates the supplied criterion-to-check mappings.
It does not use an LLM or independently establish whether those mappings are
semantically adequate. The demo uses this mode.

The optional Anthropic mode invokes Beacon and Inspector with their
[role instructions](qa_agents/prompts). Configure ANTHROPIC_API_KEY in your
environment and choose a model available to your account that supports structured
outputs:

```sh
python3 -m qa_agents run examples/request.json \
  --profile examples/profile.json \
  --provider anthropic --model YOUR_MODEL_ID
```

This is a direct API integration; Claude subscription login is not used. It sends
the task, diff, configured review files and check log excerpts to Anthropic.
There are at most two requests per run, each with a 60-second timeout, 4,096 output
token limit and 180 KB request-size limit. Usage is recorded. There is no automatic
retry or provider fallback. These limits bound requests, not a dollar budget.
[Anthropic structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

Outputs are validated locally. Invented evidence, omitted criteria, unsupported
success claims, incomplete responses and provider errors block the run. A model
cannot upgrade failed or missing mandatory evidence into a pass. Diffs and logs
that exceed the model review limits also block a pass.

## Results and recovery

Each run stores its inputs, source archive, diff, Beacon plan, check logs, Inspector
assessment, result.json and report.md under its run ID. Results include criterion
assessments, exit codes, evidence hashes, gaps and the next action. The investigation
outcome "acted" is separate from the delivery verdict.

- **Pass:** selected checks succeeded, every criterion has successful mapped
  evidence, and no gaps remain. The recorded mode identifies whether model review
  was performed. This covers the specified checks and criteria only.
- **Fail:** verification failed. This alone does not establish whether the cause
  is product code, test code or the environment.
- **Blocked:** verification is incomplete or evidence cannot support a verdict.
  Known failures remain visible even when other gaps block the review.

An identical run ID returns the saved result without executing again. Changed
requests, profiles, providers or prompts cannot reuse it. Missing or changed
evidence rejects a cached result. After an interrupted run, inspect the artifacts
and submit a new run ID. One process can run per state directory.

The caller supplies environment_id and must change it when dependencies, fixtures
or services change. Cached results describe historical evidence, not a fresh check
of the current environment. Preserve the local state directory.

## Current scope

Implemented: CLI, versioned input validation, immutable Git snapshots, check
execution, evidence storage, duplicate-run handling, Beacon/Inspector prompts,
optional Claude API calls, deterministic verdict checks and a runnable demo.

The execution backend is for trusted local repositories and profiles. Disposable
directories protect the checkout from ordinary test writes; they are not an OS
sandbox. Test programs can access the host and network. Use an isolated container
or VM runner before accepting untrusted repositories. Submodules, symlinks and
special archive entries currently block execution. Timeouts clean up the spawned
process group; independently detached processes require a stronger backend.

Scribe, Patch, Lookout, automatic repair, browser missions, GitHub publication,
dollar budgets and chief-of-staff dispatch wiring remain follow-up work. The JSON
and exit-code interface is ready for a coordinator to call; this implementation
does not update the chief-of-staff board. See the
[integration design](docs/chief-of-staff-integration.md).

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests exercise real Git snapshots and subprocesses, missing/stale evidence,
timeouts, duplicate/interrupted runs, credential environment filtering and invalid
model verdicts. Provider protocol tests use mocked HTTP responses. Live model
quality and API account access have not been validated in this build.

Optional installation for the command-line entry point:

```sh
python3 -m pip install -e .
qa-agents demo
```
