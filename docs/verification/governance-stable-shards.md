# Governance stable shard split

The R2-B governance suite accumulated 1,714 collected cases at swarm
`e0e07354`. Its unchanged 180-second shard budget expired during ordinary
continued test progress; the last displayed MCP or continuation case was not
itself evidence of a deadlock. No test timeout, whitelist, mandatory status or
network restriction is relaxed by this change.

The original `codeswarm.python.governance-projects` suite now owns the original
foundation, project/resource, sharing, cleanup/delete and fixed-history portion.
`codeswarm.python.governance-continuation` owns the remaining portion starting
at `test_continuation_source.py`. Both are mandatory members of `pr-stable` and
retain the original 180-second shard, 60-second discovery and 30-second
per-test deadlines, strict network support, and command options.

The original command selected the governance directory and several explicit
files inside it. Those explicit files remain with that directory so pytest's
existing collection deduplication does not become cross-shard duplication.
Relative input order is retained within each shard. Comparing discovered node-ID
multisets gives exactly **860 + 854 = 1,714**, with zero missing or extra cases.
Both discover and execution commands use the same partition. No production code
or test assertions change.

## Diagnostic evidence and limits

The original `8c6da409` last MCP case passed alone, and its three adjacent MCP
modules passed 83 cases. Two apparent large gaps in timestamped logs crossed
many passing cases without timestamps; attributing those gaps to their final
named test was incorrect. Those two named cases passed together under both
ordinary and strict isolation. They do not identify an authorization or MCP
lifetime defect.

A fixed `e0e07354` original-order strict run independently reproduced cumulative
expiration after 180.035939 seconds, at 81% during continuation delivery. It had
no single-test 10-second diagnostic stack dump. The split's per-test timing
records show many sub-second real-sidecar fixture setups contributing to the
aggregate. The standalone source diagnostic did not change or disable those
IO operations.

Verification uses installed noneditable core `3a3b575f632364652bdf47d55db6355134297de2`
and a swarm-only source overlay. This does not replace exact noneditable swarm
lock acceptance or the full integrated `pr-stable` run. The testctl/full-shard
tool checks also exercise their original dynamic loopback fixture; running that
fixture in a sandbox without socket capability is an environment failure, not a
new allowed failure. No real model or Provider processes are part of this change.

The split strict run passed **860 tests in 80.04 seconds** and **854 tests in
116.18 seconds** (one pre-existing Authlib deprecation warning per process).
The 18 existing testctl/full-shard tool checks passed with their owned loopback
fixture available. Collection multiset, manifest validation and diff checks
passed. No skipped cases or failure whitelist changes were introduced.
