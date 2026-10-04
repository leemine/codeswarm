# OpenCode model HTTP boundary

This slice adds a private Chat Completions consumer to the existing managed
OpenCode HTTP transport. It does not open continuation candidates or claim
complete OpenCode, Team, detached Goal, or model-provider acceptance.

## Host composition

The host selects one exact `ModelCredentialBinding` without resolving a secret.
It constructs `EngineAgentAdapter(route, model_gateway_binding=binding,
model_authority_factory=capture_original_request)`. Both keyword arguments are
optional together; omission preserves legacy construction. The synchronous
factory returns `OpenCodeModelOperationAuthority(credential_authority, use,
is_current)` using the existing `BoundCredentialAuthority` and `CredentialUse`.
It must capture the original trusted host request, identity, resource directory,
and bound model; it must not select a later active request.

The adapter calls that factory once before its logical request's first await.
It retains the exact result under the original submission receipt alongside the
existing per-Turn tool authority. Terminal projection and confirmed stop remove
it. Interactive replies use the original Turn record. Detached Goal requests
with a model gateway are explicitly unsupported; legacy Goal behavior is intact.

`ExecutionSession.bind_model_gateway(binding, authority_for_turn)` is a one-time
pre-start host port. It requires the mandatory OpenCode tool boundary and retains
the original ExecutionBinding, harness, Provider Session, and managed transport.
The original transport supplies the local gateway URL, token, and generation;
no model secret enters CLI configuration. The core private source proof binds
actual request headers/model/path to the original root, Turn, and transport.

## Actual HTTP use

Only an authenticated same-transport POST to `/model/v1/chat/completions` is
accepted. Reserved source headers must be complete and unique. The consumer
freezes the received body bytes, requires the fixed catalog model, and obtains
that source's exact per-Turn record. It checks the record's actual Provider,
host Session, subject, Workspace, credential purpose, destination, and current
ResourceGuard decision before and after credential resolution and HTTP awaits.
It never recovers a missing record from ambient authority or another Turn.

Each actual POST, including each CLI/SDK retry, resolves the credential again.
The instance-private HTTP client disables environment proxy discovery,
redirects, and transport retries. Only the upstream Bearer credential and JSON
content type are forwarded; local authorization and source headers are not.
Successful JSON or SSE chunks are checked again before and after delivery.
Provider error bodies are discarded while 4xx/5xx status is preserved for the
SDK's existing retry behavior. Denials and exceptions disclose no resolver or
transport diagnostics. Cancellation remains cancellation with a clean message.
Checks cannot retract bytes already delivered.

The existing server/listener owns lifecycle. Stop confirms the server task,
closes the exact private HTTP transport, and waits for the original Uvicorn
handler registry to drain. A failed close or pending handler keeps exit
unconfirmed and retryable. HTTPX's `is_closed` flag alone is not a close receipt.
There is no second Turn state machine, request queue, or authorization store.

## Verification and limits

`tests/unit_tests/governance/test_opencode_model_http.py` combines the production
ExecutionSession/managed ASGI transport/core source proof with a real
ProjectAccessStore, ResourceGuard, and BoundCredentialAuthority. HTTPX
MockTransport stands in for the remote model endpoint. Actual OpenAI SDK invoke,
retry, and stream calls exercise the consumer. Synthetic cases cover source and
Binding changes, revocation around awaits, chunk delivery, clean diagnostics,
first-await factory capture, and confirmed/retryable cleanup.

The candidate was tested with formally installed core
`bfe92d1d98e9afbad8edb1a42d8a64b2262d1b87` and candidate swarm source: 139 affected
tests passed. Its dependency lock is intentionally unchanged pending the parent
integration. This is not this branch's locked-pair/stable acceptance. The new
test must be included in the integrated stable manifest. A real fixed-CLI
`chat.headers` plus HTTP-source proof ordinary story, full Runtime host factory,
B3 selector/seed, and final UI acceptance remain separate integration gates.
