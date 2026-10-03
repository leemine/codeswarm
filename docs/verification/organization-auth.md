# Local shared-service organization authentication

This opt-in adapter is separate from model-account login. It supports administrator-provisioned, high-entropy, expiring access credentials for a local shared Gateway/AgentServer installation. It does not provide enterprise SSO, remote OS isolation, or complete sharing authorization.

Set `JIUWENSWARM_ORGANIZATION_AUTH_FILE` to the same absolute file in the Gateway, AgentServer, and Web static server environments **before starting them**. The JSON file must belong to the service OS user, with mode `0600` on POSIX. Its structure is:

```json
{
  "authority": "organization:your-installation",
  "signing_key": "<at least 32 random bytes, hexadecimal>",
  "credentials": [
    {
      "actor_id": "alice",
      "sha256": "<SHA-256 of a randomly generated access credential>",
      "expires_at": 1800000000,
      "revoked": false
    }
  ]
}
```

Generate each access credential with `secrets.token_urlsafe(32)` or an equivalent CSPRNG. SHA-256 here is for random access credentials, **not passwords**. Supply credentials to users privately. Do not commit the configuration, signing key, access credentials, or browser cookies. Use a distinct signing key/authority per installation. The configured credential expiry is a Unix timestamp. Keep actor IDs stable; replacement of an actor requires new credentials.

The existing browser login page selects organization login when `/api/v1/auth/organization/status` reports it enabled. The user enters the provisioned credential; the HTTP endpoint verifies it and sets an HttpOnly, SameSite=Strict cookie (`Secure` on HTTPS). Clients without a cookie jar can send `Authorization: Bearer <credential>`. Credentials are never taken from URL query parameters or `X-User-Id`.

Signing out revokes the current access credential in the configuration, then clears the browser cookie. Signing in again requires a newly provisioned credential. A read-only/unwritable configuration causes a visible sign-out failure; the application does not claim revocation succeeded. Revocation uses the existing `portalocker` dependency and a stable `.lock` sidecar with atomic owner-only replacement. Administrative writers must coordinate on that same lock, rather than concurrently rewriting stale configuration snapshots. Configuration is the sole authority; no second account/session database is introduced.

Gateway rechecks authentication before processing each Web/TUI request. The queued inbound path captures each request's context; it never infers authority from a routing user ID. The AgentServer transport receives short-lived HMAC assertions bound to the canonical request body, audience, nonce, credential digest, actor, and authority. The receiver checks current configuration and rejects tampering, expiration, replay, and assertions issued before that receiver's startup. The assertion is removed before existing E2A parsing. It is a transport envelope extension, not a new harness protocol or persisted user metadata. Internal service requests may have no human identity; they do not fall back to the local installation identity.

The synchronous Runtime resolver consumes `current_identity()`, which rechecks the current credential on every call. Tasks inherit the credential reference, not a permanent authorization decision. Browser/ASGI delivery and queued WebSocket output recheck before each outgoing chunk/frame. Work already admitted still requires Runtime/Provider cancellation and authorization checks; authentication alone does not revoke an in-flight external command.

The static server's legacy `/file-api/*` and `/share-api/*` operate on installation-wide paths. They are deliberately unavailable in organization mode until project/share-scoped file authorization is connected. Existing single-user behavior remains unchanged when the environment variable is absent. Do not advertise full organization sharing, full session ownership isolation, or all-channel protection from this authentication slice alone.

Organization mode also refuses recognized slash controls on the existing controlled IM channels before reading or changing channel state, starting cancellation tasks, or preparing local commands. Those routes do not yet have trusted organization channel-state, resource, or Team-seat mappings. This includes mode/session controls, review commands, and join/exit; authenticating a principal alone does not enable them. Ordinary messages still follow the request authorization path, and ordinary Web requests are unaffected by this control gate. Direct rewind helpers cannot fall back to local shared-directory history in organization mode. Legacy single-user controls keep their existing behavior. Each queued Message has its own host-only principal reference, even when the producer reuses a Message instance; that reference is not a dataclass/wire field, and consumption/signing recheck live credentials.

Validation is recorded per implementation commit. Deterministic tests cover two independent credential contexts, forged routing IDs, request tampering/replay, expiry/revocation/restart, queue propagation, direct AgentServer rejection, HTTP login/logout, failed logout, and revoked streamed output. Browser UI and the real project sharing/Provider story require integration acceptance in addition to these tests.

Archived logs preserve output with trailing whitespace removed.

## 2026-10-03 local candidate validation

Base swarm SHA: `1eefab4163ce280f622cde0b30f7c5cf5c00da57`. Python 3.13.15 is `/tmp/r1-12-quality/locked-venv/bin/python`; swarm imports this worktree, and core imports its existing site-packages installation. This is **local source validation**, not a fresh locked install or CI source attestation.

- `JIUWENSWARM_CONFIG_URL=http://127.0.0.1:9 JIUWENSWARM_HOME=/tmp/r2b-auth-tests-host/home JIUWENSWARM_DATA_DIR=/tmp/r2b-auth-tests-host/data /tmp/r1-12-quality/locked-venv/bin/python -m pytest tests/unit_tests/governance/test_organization_auth.py tests/unit_tests/test_agentos_ws_handshake_auth.py tests/unit_tests/gateway/test_web_http_auth_routes.py tests/unit_tests/agentserver/test_agent_ws_connection_close.py --no-cov -q --disable-warnings --timeout=30`: **66 passed**, 1 warning, exit 0. [Log](evidence/organization-auth/python-tests.txt).
- Frontend `npm run build -- --outDir /tmp/r2b-auth-web-dist`: exit 0, including TypeScript; existing large-chunk warning. Reused installed frontend dependencies; no install or lock change. [Log](evidence/organization-auth/frontend-build.txt).
- Frontend `node --test tests/i18nLocales.test.mjs tests/authStore.test.mjs`: 2 passed, exit 0. [Log](evidence/organization-auth/frontend-tests.txt).
- New Python module/tests `ruff check`, and `git diff --check`: exit 0.

Earlier attempts: sandbox Starlette TestClient blocked and was interrupted; a no-coverage async-only retry passed. The first host adjacent-suite run used global `JIUWENSWARM_CONFIG_URL=off`, which made two existing campaign-state tests observe `off` instead of their mocked campaign state (61 passed, 2 failed). Replacing that test-process configuration with a loopback URL allowed their mocked configuration to operate; subsequent expanded suite passed. No product behavior or test expectation was weakened. The first frontend invocation lacked the dependency symlink and failed `tsc: not found`; after reusing the installed dependencies, full build passed. An initial Ruff command used the frontend working directory with repository-relative paths and failed file lookup; the repository-root command passed.

Not yet run for this slice: interactive browser visual QA, two-browser project sharing story, real Provider execution acceptance, stable CI, clean locked-source installation, Team acceptance. These remain integration gates; authentication component tests do not close R2-B2.
