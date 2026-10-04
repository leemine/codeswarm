# Owner Workspace artifact download UI

This frontend slice is based on swarm `b4f73f92dccb37b11e05e66250d8692e2392abd8`. It does not change the core dependency (`3a3b575f632364652bdf47d55db6355134297de2`), Runtime, Session, server issuers, RPC or HTTP authorization.

## Behavior

The existing ArtifactsPanel download button receives the trusted organization-mode flag and explicit private Session ID from App. An organization operation requires the chat store's active Session to match and the current WebSocket connection to be ready. It accepts only an opaque token with the relative `/file-api/download` route, fixes `session_id` to the captured App Session, and rejects absolute URLs, path/raw-file fallbacks, unknown or duplicate query fields and conflicting tokens or Session IDs. Token contents are not authorization; the server must verify its own issuer, original Binding/source and current permissions.

Browser retrieval uses same-origin credentials, same-origin mode, no cache and no redirects. The full response is read before saving. Session, connection or credential-change notifications synchronously invalidate the operation generation and abort its fetch; late fetch/body results are discarded. Unmount and changed App identity/Session context also invalidate it. Error bodies are not read or displayed. Failure uses the existing download alert with generic bilingual text.

Desktop retrieval also happens in the authenticated browser. It calls the existing blob-save transaction, never the legacy Python URL downloader. Optional save guards check before and after asynchronous preparation and before append/commit; a stale transaction is aborted using its original transfer ID. Browser object URLs are released. These checks reject a stale UI operation; they cannot retract already downloaded or saved bytes. Unmodified legacy callers retain their previous API and behavior.

The ToolPanel overview entry also reads the same organization context: organization clicks open the existing artifact panel, while legacy desktop clicks retain their native browser behavior. Two actual ToolPanel click tests cover both modes.

Organization previews in this panel currently show a download instruction, including when a previously selected artifact is revisited. They cannot enter path-only preview or the desktop file browser. Ordinary Workspace downloads are the only newly wired surface. Temporary/sealed files, shared-history attachments, trace exports and Git exports remain unavailable. Organization preview restoration and the full capability-preservation exit remain **pending**, not completed by these notices. Shared-history text rendering is unchanged.

## Verification and limits

From `jiuwenswarm/channels/web/frontend`:

- `npm run test:owner-artifact-download`: 17 passing real React/jsdom and routing/save behavior cases. Covers allowed browser and desktop saves, fixed Session routing, rejected URLs/parameters, stale body responses, Session/connection/identity invalidation, original desktop abort, unavailable path artifacts, preview gating, generic failure feedback and unchanged legacy URL save.
- `npm run test:desktop-save`: 11 passing cases, including existing desktop transactions and guarded browser-picker behavior.
- `npm run test:artifact-collection`: 5 passing cases.
- `npm run test:i18n-locales`: 4 passing cases.
- `JIUWENSWARM_WORKSPACE=/tmp/r2b-owner-download-ui/build-workspace npm run build`: passed; existing bundle-size warnings retained.
- `git diff --check`: passed.

Evidence: `/tmp/r2b-owner-download-ui/`. The initial new test fixture incorrectly used `files` instead of `fileItems`; its preview renderer also needed the repository's decorative SVG test loader. Both fixture failures were preserved before correction. They were not product or authorization results.

A bounded, task-owned loopback page rendered the actual modified components with synthetic store state, compiled CSS and Chrome at widths 1000 and 584. Both list and preview-notice states passed without horizontal overflow; screenshots were inspected. This is visual component evidence, **not** Gateway/AgentServer download acceptance. It used the current default light theme; no theme behavior changed. The loopback server and browser were closed. No real Provider or user credentials were used.

The frontend dependencies reuse the existing local node_modules installation; this is source/component validation, not a fresh dependency installation or a Python locked-source claim. The new `test:` script is discoverable by the repository's web-scripts runner; web scripts are a separate diagnostic profile rather than the Python pr-stable gate. Final integrated stable, backend pairing, real authenticated browser download and desktop bridge acceptance remain the integration owner's responsibility.
