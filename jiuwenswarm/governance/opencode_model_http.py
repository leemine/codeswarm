"""Private OpenCode model HTTP consumer; grants remain in ResourceGuard."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Callable

import httpx

from .credential_resources import BoundCredentialAuthority, CredentialUse
from .model_credentials import ModelCredentialBinding
from .resources import ResourceAccessDenied

_MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT = 180


@dataclass(frozen=True, slots=True)
class OpenCodeModelOperationAuthority:
    credential_authority: BoundCredentialAuthority
    use: CredentialUse
    is_current: Callable[[], bool]

    def __post_init__(self):
        if (
            type(self.credential_authority) is not BoundCredentialAuthority
            or type(self.use) is not CredentialUse
            or self.use.purpose != "model"
            or not callable(self.is_current)
        ):
            raise ValueError("explicit model credential authority required")


def model_authority_current(value):
    cancelled = False
    try:
        return (
            type(value) is OpenCodeModelOperationAuthority
            and value.is_current() is True
        )
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass
    if cancelled:
        raise asyncio.CancelledError()
    return False


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate model request field")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("nonfinite model request")


class OpenCodeModelHttpConsumer:
    """One original Binding's client, owned by the existing HTTP transport."""

    def __init__(
        self,
        binding,
        *,
        execution_binding,
        capture_source,
        is_source_current,
        authority_for_turn,
        is_current_transport,
    ):
        if (
            type(binding) is not ModelCredentialBinding
            or execution_binding.provider_id != "opencode"
        ):
            raise ValueError("fixed OpenCode model binding required")
        self.binding = binding
        self._execution = execution_binding
        self._capture = capture_source
        self._source_current = is_source_current
        self._authority = authority_for_turn
        self._transport_current = is_current_transport
        self._destination = binding.api_base + "/chat/completions"
        self._close_confirmed = False
        self._http_transport = httpx.AsyncHTTPTransport(retries=0)
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            transport=self._http_transport,
            timeout=30,
        )

    @property
    def closed(self):
        return self._close_confirmed

    async def close(self):
        if self._close_confirmed:
            return
        await self._client.aclose()
        # HTTPX sets is_closed before awaiting its transport. Retain the exact
        # transport so a partial/failed close is rechecked on the next stop.
        await self._http_transport.aclose()
        self._close_confirmed = True

    def _check(self, source, authority):
        if (
            self.closed
            or self._client.is_closed
            or self._transport_current() is not True
            or self._source_current(source) is not True
            or type(authority) is not OpenCodeModelOperationAuthority
            or self._authority(source.turn_id) is not authority
            or not model_authority_current(authority)
        ):
            raise ResourceAccessDenied("model request unavailable")
        execution = authority.credential_authority.execution
        expected = self._execution
        if (
            execution.provider_id != expected.provider_id
            or execution.session_id != expected.host_session_id
            or execution.identity.subject_id != expected.subject_id
            or execution.workspace != expected.workspace
            or authority.use.destination != self._destination
            or source.model != self.binding.model
        ):
            raise ResourceAccessDenied("model execution mismatch")
        decision = authority.credential_authority.check_for_request(
            authority.use, destination=self._destination
        )
        if (
            authority.credential_authority.execution is not execution
            or self._source_current(source) is not True
            or self._transport_current() is not True
            or self._authority(source.turn_id) is not authority
            or not model_authority_current(authority)
        ):
            raise ResourceAccessDenied("model authority changed")
        return decision

    async def handle(self, scope, receive, send, source_headers):
        """Transport has authenticated host/token and rejected duplicate headers."""
        started = False
        cancelled = False
        try:
            async with asyncio.timeout(_TIMEOUT):
                data = bytearray()
                while True:
                    message = await receive()
                    if (
                        self._transport_current() is not True
                        or message.get("type") != "http.request"
                    ):
                        raise ResourceAccessDenied("model input unavailable")
                    data.extend(message.get("body", b""))
                    if len(data) > _MAX_BYTES:
                        raise ValueError("model input limit")
                    if not message.get("more_body", False):
                        break
                body = bytes(data)
                payload = json.loads(
                    body, object_pairs_hook=_object, parse_constant=_nonfinite
                )
                if (
                    not isinstance(payload, dict)
                    or payload.get("model") != self.binding.model
                    or type(payload.get("stream", False)) is not bool
                ):
                    raise ValueError("model input unavailable")
                source = await self._capture(
                    source_headers,
                    method=scope["method"],
                    path=scope["path"],
                    model=payload["model"],
                )
                if source is None:
                    raise ResourceAccessDenied("model source unavailable")
                authority = self._authority(source.turn_id)
                before = self._check(source, authority)
                secret = await authority.credential_authority.resolve_for_request(
                    authority.use, destination=self._destination
                )
                if self._check(source, authority) != before:
                    raise ResourceAccessDenied("model authority changed")
                # Immutable bytes and host URL; local token/source headers never go upstream.
                request = self._client.build_request(
                    "POST",
                    self._destination,
                    content=body,
                    headers={
                        "Authorization": "Bearer " + secret,
                        "Content-Type": "application/json",
                    },
                )
                secret = None
                if (
                    str(request.url) != self._destination
                    or request.method != "POST"
                    or request.content != body
                    or self._check(source, authority) != before
                ):
                    raise ResourceAccessDenied("model sink changed")
                response = await self._client.send(request, stream=True)
                try:
                    if self._check(source, authority) != before:
                        raise ResourceAccessDenied("model response unavailable")
                    if response.status_code != 200:
                        # Preserve SDK retry status without disclosing provider error bodies.
                        if not 400 <= response.status_code <= 599:
                            raise ResourceAccessDenied("unsupported model redirect")
                        await response.aclose()
                        if self._check(source, authority) != before:
                            raise ResourceAccessDenied(
                                "model error delivery unavailable"
                            )
                        await send(
                            {
                                "type": "http.response.start",
                                "status": response.status_code,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        started = True
                        if self._check(source, authority) != before:
                            raise ResourceAccessDenied(
                                "model error delivery unavailable"
                            )
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b'{"error":{"message":"Model provider unavailable","type":"provider_error"}}',
                            }
                        )
                        return
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    expected = (
                        "text/event-stream"
                        if payload.get("stream", False)
                        else "application/json"
                    )
                    if content_type != expected:
                        raise ValueError("unsupported model response")
                    # Do not forward arbitrary upstream headers (cookies, tokens, redirects).
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 200,
                            "headers": [(b"content-type", expected.encode())],
                        }
                    )
                    started = True
                    if self._check(source, authority) != before:
                        raise ResourceAccessDenied("model delivery unavailable")
                    total = 0
                    async for chunk in response.aiter_bytes():
                        if self._check(source, authority) != before:
                            raise ResourceAccessDenied("model delivery unavailable")
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            raise ValueError("model output limit")
                        await send(
                            {
                                "type": "http.response.body",
                                "body": chunk,
                                "more_body": True,
                            }
                        )
                        if self._check(source, authority) != before:
                            raise ResourceAccessDenied("model delivery unavailable")
                    if self._check(source, authority) != before:
                        raise ResourceAccessDenied("model completion unavailable")
                    await send(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
                finally:
                    await response.aclose()
                return
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        # Outside handlers: transport/credential exceptions may contain secrets.
        if cancelled:
            raise asyncio.CancelledError()
        if started:
            raise RuntimeError("model response could not be authorized")
        body = b'{"error":{"message":"Model request unavailable","type":"permission_error","code":"model_authority_denied"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
