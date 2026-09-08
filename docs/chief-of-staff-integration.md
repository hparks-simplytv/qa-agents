# Chief-of-staff integration proposal

2026-09-08. Operating design for Haley's work fork, based on the public QA Agents role concepts. The first implementation now provides a CLI, evidence collection, Beacon/Inspector prompts and optional model review. This document also describes planned extensions; see the [README](../README.md) for the exact implemented scope. The runtime is newly built, not a reconstruction of the withheld private system.

Haley authorized using the public repository and filling in the missing details. Keep the upstream MIT license and attribution. The chief-of-staff coordinator owns task scheduling, execution receipts and GitHub publication. This repository should provide QA roles and a small runner interface.

## First implementation

Start with a deterministic evidence collector, Beacon and Inspector. Add Scribe when regression-test generation is needed, Patch for test-defect investigations, and Lookout for browser workflows. Do not invoke all five roles for every change.

The engineering manager supplies an integrated candidate. The QA manager compares it with the original accepted criteria, returns a verdict and routes concrete findings back to engineering. It does not accept the implementer's completion summary as test evidence.

## Role instructions

| Role | Input and task | Required result |
| --- | --- | --- |
| Beacon | Read the accepted brief, candidate diff and application profile. Identify changed behavior, relevant risks and the smallest evidence plan. Select specialists only when needed. | Criteria-to-check mapping, requested evidence, chosen investigations and any ambiguity that blocks evaluation. |
| Inspector | Inspect actual test results, diff, coverage when available and specialist evidence. Compare each acceptance criterion with observations. Distinguish a defect from absent evidence. | Per-criterion supported/failed/unverified assessment, reproducible findings and delivery verdict. |
| Scribe | Given accepted expected behavior and a specific coverage gap, draft a focused regression test in a separate worktree. Do not infer product requirements from current implementation. | Test patch, expected assertion and actual execution evidence from the verification runner. Unrun tests remain explicitly unverified. |
| Patch | Investigate a failing test using logs, expected behavior and reproduction. Determine whether the cause is product behavior, test code or environment. | Diagnosis with evidence. A test-only repair may be proposed; product fixes return to engineering. Never weaken an assertion merely to make it pass. |
| Lookout | Explore the specified workflow against the candidate's test environment using bounded test data. Follow the mission's allowed interactions. | Reproduction steps, observed versus expected behavior, screenshots/traces where useful, and untested paths. A blocked environment yields blocked evidence. |

The QA manager aggregates these results. Specialist narrative alone cannot produce a pass: every required criterion needs applicable evidence and every mandatory check must have completed successfully. QA can identify missing expected behavior, but product/owner context supplies the answer.

## Proposed runner contract

One request file and one result file are sufficient for the first adapter. These are proposed fields, not an existing API:

- Request identity: `task_id`, `run_id`, `attempt`, `brief_version`.
- Candidate identity: repository, base commit, candidate commit or immutable snapshot digest, worktree, test environment/build identifier.
- Task: original outcome, accepted criteria, constraints and relevant project context references.
- Verification profile: named test/build/browser checks, fixtures and permitted interactions. The runner resolves approved check names; model output does not become an arbitrary shell command.
- Limits: deadline, remaining cost budget, selected roles and artifact directory.

Return matching identities, `verdict` (`pass`, `fail`, `blocked`), criterion assessments, check names and exit results, artifact references, findings, unresolved gaps, usage and recommended next action. Preserve the public investigation outcome (`acted`, `blocked`, `abstained`) separately where useful. `acted` does not imply a delivery pass.

Verdict rules:

1. `fail`: a verification check failed. Diagnosis must still establish whether the cause is product behavior, test code or environment; the runner does not infer causality from an exit code.
2. `blocked`: evidence is absent, stale, incomplete, contradictory, or cannot be collected; expected behavior is unresolved; or the investigation requires unavailable authority. Report known defects even when the overall evaluation is blocked.
3. `pass`: required checks succeeded, accepted criteria have supporting evidence and no unresolved blocking findings remain. State the scope of verification; this is not a claim of complete defect absence.

The result must identify the exact tested candidate. Changes to code, accepted criteria or relevant environment invalidate the corresponding assessment. The publisher must verify candidate identity before attaching results to GitHub.

## Execution and repair loop

The coordinator creates the run receipt, then invokes the QA runner. The runner gathers deterministic evidence before asking roles to interpret it. Persist artifacts before returning a result, so a coordinator restart can reconcile a completed run. A repeated run ID returns its recorded state rather than launching duplicate work.

For the pilot, use one active QA run and at most two automatic engineering repair attempts per task. These are proposed defaults. Reuse collected evidence only if its candidate and environment identities still match. On limit exhaustion, return the findings to the chief of staff with the unresolved decision. Parent cancellation stops outstanding QA work.

Keep QA test edits separate from the integrated candidate until engineering incorporates them. Once incorporated, produce and verify a new candidate. The runner should use an isolated execution environment with test credentials; application code and test commands are executable code even when the command names are fixed.

The QA runner writes local evidence and results. The coordinator's publisher creates or updates the GitHub PR and check. Merge behavior follows repository policy. This separates quality evaluation from publication and avoids giving each specialist its own GitHub task lifecycle.

## First demonstration

Use a small controlled application change involving quantity and discount behavior, following the public Little Bytes scenario. This is a proposed experiment; the static upstream artifact is not its execution result.

1. Supply accepted expected behavior and an actual candidate revision.
2. Run the existing checks and demonstrate the relevant missing regression evidence.
3. Have Beacon frame the gap and Inspector report it without declaring pass.
4. Have Scribe draft a regression test from the accepted behavior; execute it using the verification runner.
5. If behavior is wrong, return the defect to engineering, integrate the fix, and rerun QA on the new revision.
6. Return a review package with the real logs, test patch and verdict.

Adapter acceptance tests should cover stale evidence, missing logs, a failed required check, an ambiguous requirement, a duplicate run request and an interrupted run. The first live model evaluation should also check that Inspector refuses to mistake a confident engineering summary for verification.

## Implementation order

1. Define and validate the request/result format and one application verification profile.
2. Build the evidence collector and isolated verification runner.
3. Implement Beacon/Inspector invocation using the existing worker/provider integration where practical.
4. Add the coordinator's QA stage and bounded repair routing.
5. Run the controlled demonstration, then one real work-repository change.
6. Add Scribe, Patch and Lookout as the selected changes require them.

The provider and model can be selected during the first live experiment. The contract should not embed a model name or require a new orchestration framework.
