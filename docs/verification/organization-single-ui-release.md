# Organization Single UI release boundary

This candidate uses the existing trusted `organizationAuth` login result. Organization sessions hide Goal mutation controls, disable Team mode, and reject Goal mutation/attach and Team execution at the existing send points. Goal `get`, ordinary Single messages and their approval replies remain available. Stale Goal drafts are preserved rather than sent as ordinary chat. Retained callbacks read the current authentication mode.

The existing organization-only Codex settings restriction and reason remain unchanged. Nonorganization Goal, Team and Codex behavior remains unchanged; no saved configuration is rewritten. Historical records remain available.

This UI boundary is not authorization. Runtime/API rejection remains necessary. It does not close R2-B or the missing real negative permission checks and active External credential revocation/exit verification. Goal, Team and full Provider preservation are not declared complete by hiding controls.

## Candidate verification

Base: Swarm `7c0461c190684c5b342ebb0477d34a78bbb050ee`, frontend tree `30b434676af947b1f68b196571638fb739affb43`. This package changes frontend only; it does not change or import the core lock. Tests use local existing Node dependencies with an isolated build/test cache; no real Provider, user credentials or network fixture is used.

- `npm run test:input-area-permission-merge`: original permission behavior plus actual organization DOM and hook/transport checks. The hook uses an in-memory WebSocket and rejects unexpected HTTP.
- `npm run test:codex-governed-availability`: existing governed restriction and legacy behavior.
- `npm run test:i18n-locales`: bilingual resource checks.
- `npm run build`; `git diff --check`.

The first command was accidentally launched at the repository root and reported missing package.json. The first DOM run lacked the real `.chat-panel-shell` ancestor required by the existing alert portal; the fixture now includes it and keeps the visible-alert assertion. These failures are not product failures. Logs are `/tmp/r2b-single-ui-*.log`.

No new real UI or stable run was performed in this independent package. Prior Native/OpenCode Single evidence remains tied to its original SHA; integration requires the parent's new source-pair checks.
