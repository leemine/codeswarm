# M2 Code controls and independent verification

This candidate integrates the local M2 fixes with develop at
`7d573527c46f179b3920f46cbbc9805f694abd7d`. The locked Core remains
`91943c6b6d2aa518eb8c5cd03a382c5afdc38e06`. It preserves Taskboard,
Provider selection, the existing Runtime/SerializedTurnHarness, and the original
approval controls. Integration resolved only the package.json test-script list,
retaining both reasoning-display and Taskboard scripts.

## Behavior changes

- Evaluation event deltas no longer trigger redundant status writes that delay
  Code events. Long reasoning display updates are coalesced.
- Answered Native questions disappear during resumed streaming and restoration.
- Current typed failures settle the chat UI; failures from an older request do
  not settle a newer request. Declined interactions, execution event-byte limits,
  and model output limits have readable live and restored messages.
- Failed external executions are retired through the original strict stop chain.
  Same-session follow-up admission waits for confirmed cleanup and history;
  unconfirmed cleanup retains ownership and can be retried by explicit stop.
- Independent verification attributes ordinary delivered-module Exceptions to
  test_failed, including truthful zero-assertion import-time failure. Authority
  errors, missing dependencies, non-Exception base exceptions and invalid reports
  remain environment errors or fail closed. Existing timeout/cancel/cleanup
  ownership and legacy positive-assertion report compatibility remain.
  Integration review also covered an explicit AssertionError raised by delivery
  import before any authority assertion. It now carries delivery provenance and
  remains test_failed with zero assertions; an authority-only explicit
  AssertionError with zero assertions still fails closed as environment_error.

## Evidence and limits

Formal model runs used local Swarm `47420d3b93f2859cefb7cf941ac2f55e64742c9e`
and the locked Core above. Deepagent and OpenCode each solved the frozen Stockflow
task once within its original 1200-second deadline. Both deliveries passed the
platform's automatic independent Docker acceptance with 146 actual assertions.
The ledger benchmark was not solved again.

The real codeswarm task was a seven-file **module snapshot**, not a full repository
checkout. Independent scoring remained outside the model workspace; only the
declared independent.py delivery was exported. Calibration covered the reference,
original defect, and nine wrong/tampering/dependency variants. The reference
passed 506 assertions and every negative variant was rejected.

The first formal Deepagent delivery failed on GeneratorExit attribution after
315 executed assertions; 315 is not a passed-assertion count. OpenCode exhausted
the default 32000 generated-token budget of one model request (31990 reasoning
plus 10 output), made no delivery changes, and did not reach formal acceptance.
These are failed model results, not passing platform tests. Neither engine was
given another formal solve. A later scoring correction removed a private-field
name assumption; it was published as a new v2 task and calibrated separately.
Its same-delivery diagnostics do not overwrite the original results, and v2 has
not received another formal model evaluation.

Local repair endpoint `ecc9970642721c4d1b6490423ffc1035abaec756` passed
18 strict-stable suites / 6009 checks with closed accounting. Actual UI evidence
covered questions, active-tool cancellation with host process exit and no late
end marker, same-session continuation, and restored tool/approval history on
Deepagent/OpenCode. A new rejection-then-follow-up probe verified strict failed
execution cleanup and subsequent OpenCode admission without a manual stop.
Seven real offline Docker cases verified success, delivery runtime/import/syntax
failure, authority failure, missing dependency and GeneratorExit classification,
including confirmed exit and container removal.

Those results belong to their exact historical versions. The integrated candidate
must pass its own affected tests and remote stable/locked-source checks before
merge; the merged SHA must be checked separately. CI now also executes the
question-queue, terminal-error, reasoning-display and history scripts explicitly.

Detailed experiment IDs, commands, source hashes, negative runs and preservation
evidence are maintained in the management repository report
`docs/verification/EVALUATION_COMPLEX_REPO_20261010.md` and the merge report
`docs/verification/EVALUATION_M2_MERGE_20261011.md`. No credentials, model traces,
user configuration or user-directory data are part of this candidate.
