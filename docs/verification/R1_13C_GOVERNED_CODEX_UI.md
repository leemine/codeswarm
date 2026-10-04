# Codex required tool authorization: UI availability

2026-10-04. This UI slice explains an existing execution blocker. It does not
complete R1-13C, R2-B, Native/OpenCode qualification or the required three-Provider
acceptance matrix. No CLI update or real Provider probe is part of this change.

| Existing execution mode | Mandatory tool authority | Codex behavior / UI |
| --- | --- | --- |
| Legacy single-user configuration | No explicit mandatory callback | Existing toggle, CLI detection and deferred-install replay stay unchanged. Ordinary approval settings are not mandatory per-operation resource authority. |
| Authenticated organization managed Session | Runtime installs required resource callbacks for managed project ACL revision > 0 | Codex construction fails closed; the existing Team external-CLI settings show an unavailable reason and disable Codex editing/detection/replay. Stored configuration is retained. |
| Explicit embedding with a required callback, without the organization Web shell | Host-defined | Core still rejects; this organization UI projection does not detect or advertise support for that separate embedding. |

## Evidence and source of the distinction

The fixed Codex CLI 0.144.4 has native tool paths (including view_image) outside
host approval coverage. Its hook failure/timeout behavior is not a trustworthy
mandatory gate. Core CodexHarness._validate_context rejects non-null
tool_authorizer before execution; removing the UI restriction does not authorize
execution. Installing another path, changing a model or toggling ordinary approval
must not be presented as a solution. There is no automatic Provider fallback.

The UI reuses AppWithAuth's existing same-origin
/api/v1/auth/organization/status result, passed as organizationAuth through the
existing SettingsPage/SettingsServicesProvider. It does not infer organization
mode from legacy IAM authStore.enabled or permissions.enabled. Organization
Session ownership requires an explicit managed project in sharing_host._project;
Runtime._resource_authorizers_for binds mandatory callbacks for that positive ACL
revision. The old approval/full_access flag does not remove this resource boundary.

This checkout has no ExecutionSettings/profile-selection preflight RPC or
blocked_code field. The only existing Codex settings control is the experimental
ExternalCliAgentsSection. Its unavailable notice is therefore scoped to that
surface. The post-start SurfaceCapabilityManifest remains a runtime capability
projection, not a construction-time security decision. B3 options still return
only the existing Native candidate intersection; no extra DTO fields or empty
Codex candidates were added to the sharing contract.

## Compatibility and validation

The org-only notice hides stale detection success, stops automatic/manual Codex
detection and does not send Codex updates from manual save or deferred replay.
Existing configuration values and deferred choices are not silently rewritten.
Claude and legacy Codex behavior remain unchanged; this is not a support claim
for governed Claude or any other Provider. UI is advisory; actual enforcement is
still in the original Runtime/core gates.

New real React/jsdom tests exercise the control and existing Settings service /
config-source call chain, including retained checked state, no background Codex
detection, mode changes, blocked pending replay/save, unchanged legacy replay and
other-agent updates without Codex configuration writes. Existing install-state
and bilingual locale tests and npm build are run separately. Local visual review
uses the actual component and generated CSS with synthetic props, not an
organization API/Provider end-to-end validation. Exact commands/results and
screenshots are archived with the candidate review evidence.

Validation result for this slice: 5 new React cases, 12 legacy install-state
cases and 4 locale cases pass; production build passes. The wider settings-refactor
file has 32 passing cases and one baseline failure: its old browser-locale assertion
expects only `errors`, while base 8c6 already contains `errors` and `pane`. The same
exact failure was reproduced on a detached base checkout; no whitelist or test
expectation was changed. Local visual checks pass for organization/legacy controls
at 1000px and the existing 584px minimum width, with the shipped default light
theme. This checkout ships no default dark theme; dark rendering is unverified.
The first 390px fixture exceeded the application's existing min-width and is
retained as a fixture limitation, not reclassified as a product success.
