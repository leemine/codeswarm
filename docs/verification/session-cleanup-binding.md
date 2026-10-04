# Cancel uses the persisted cleanup resource binding

An organization cleanup request has two different channels: the original wire
channel for request integrity and the response, and the channel persisted in the
Session's resource binding. Previously cancel selected the AgentManager cache
using the wire channel. A request arriving on another channel could therefore
close the Coordinator and return success without stopping the persisted owner.

`SharingHostService.cleanup_owner_binding(session_id, identity,
expected_stamp=...)` now returns an immutable projection of the original durable
metadata under the existing owner sidecar lock. Its fields are `project_id`,
`project_dir`, `mode`, `work_mode`, `team_name`, `channel_id`,
`execution_profile_id`, and `execution_config_fingerprint`. The private
`_metadata_cleanup_binding` helper is the common projection seam for deletion
integration. Project, channel and mode must be explicit, nonempty strings; Team,
missing metadata, invalid types, changed owner or changed metadata deny cleanup.
The existing opaque nine-component owner stamp is unchanged.

`SessionCleanupAuthority` pins that projection, its resource channel, the
original wire channel, and the existing owner/execution facts. Every check
revalidates all of them. Cache lookup, subagent release, strict stop, cleanup and
execution-owner removal use only the resource channel. Each awaited step is
followed by a check, including execution-owner removal. The response uses the
original wire channel. This authority grants no history, model, tool or general
execution access; source revocation still permits only the original owner's
cleanup.

The original AgentManager subagent release port also stops choosing the first
Agent on a channel. It scans existing cached objects for the exact Session and
checks cache/adapter identity across awaits. It never allocates, borrows or
selects a default Agent. A replaced current or later cache entry makes the
original release fail rather than touching the replacement. An absent Session
remains a no-op. This preserves the original Runtime generation fence rather
than adding a scheduler or persistent store.

## Evidence and limits

`tests/unit_tests/runtime/test_session_cleanup_binding.py` exercises the original
Runtime/Coordinator and actual AgentManager caches with synthetic cached Session
owners. Its pre-fix regression returned success while leaving the persisted
channel's owner running. It now stops only that owner, releases the matching
subagent runtime even when it is not first on the channel, and returns on the
original wire channel. Other cases cover every frozen binding field, missing
metadata, invalid/Team bindings, wire mutation, metadata changes during each
await, exact cache replacement, and cleanup retries after a synthetic fault.

These tests use temporary metadata/sidecars and synthetic execution, not a real
Provider. Candidate CI and the integrated permanent-delete/real UI story remain
separate acceptance gates. No protocol, Provisioner, SessionArchive, Runtime
service, lifecycle lock, or persistent schema was changed by this slice.
