# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit local shared-service authentication; disabled by default.

The administrator provisions expiring, high-entropy access tokens. Only their
SHA-256 digests are stored. This is not the model-account login or remote OS
isolation. The same private configuration is installed in Gateway/AgentServer.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any

import portalocker

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)

CONFIG_ENV = "JIUWENSWARM_ORGANIZATION_AUTH_FILE"
COOKIE = "jiuwenswarm_organization"
ASSERTION = "_organization_assertion"
_current: contextvars.ContextVar[AuthenticatedPrincipal | None] = (
    contextvars.ContextVar(
        "organization_principal",
        default=None,
    )
)


@dataclass(frozen=True, repr=False)
class AuthenticatedPrincipal:
    config_path: str
    credential_digest: str
    bound_identity: TrustedIdentity

    def identity(self) -> TrustedIdentity:
        identity = OrganizationAuthenticator(self.config_path).resolve_digest(
            self.credential_digest
        )
        if identity != self.bound_identity:
            raise PermissionError("organization identity changed; authenticate again")
        return identity

    def __repr__(self) -> str:
        return "AuthenticatedPrincipal(<redacted>)"


class OrganizationAuthenticator(CredentialAuthenticator):
    """File-backed minimal credential supply, reloaded at every authority check."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._seen: dict[str, float] = {}
        self._started_at = time.time()

    def _config(self) -> dict:
        # Fail closed on deletion, malformed content, expiry, or permissions.
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("organization authentication unavailable")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError(
                "organization authentication config must be owner-only"
            )
        config = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(config.get("credentials"), list):
            raise PermissionError("invalid organization authentication configuration")
        if len(bytes.fromhex(config["signing_key"])) < 32:
            raise PermissionError("invalid organization signing key")
        return config

    def revoke(self, principal: AuthenticatedPrincipal) -> None:
        if principal.config_path != str(self.path):
            raise PermissionError("organization authentication authority mismatch")
        # A stable sidecar lock coordinates processes across atomic replacement.
        # The configuration remains the sole authority; there is no session DB.
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with portalocker.Lock(str(lock_path), timeout=5):
            config = self._config()
            if not self.path.stat().st_mode & stat.S_IWUSR:
                raise PermissionError("organization authentication config is read-only")
            for entry in config["credentials"]:
                if hmac.compare_digest(
                    str(entry.get("sha256", "")), principal.credential_digest
                ):
                    entry["revoked"] = True
            fd, temporary = tempfile.mkstemp(
                prefix=".organization-auth-", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(config, stream, ensure_ascii=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def resolve_digest(self, digest: str) -> TrustedIdentity:
        config = self._config()
        for entry in config["credentials"]:
            if hmac.compare_digest(str(entry.get("sha256", "")), digest):
                expiry = float(entry["expires_at"])
                if (
                    entry.get("revoked", False)
                    or not math.isfinite(expiry)
                    or expiry <= time.time()
                ):
                    break
                return TrustedIdentity(
                    entry["actor_id"], entry["actor_id"], config["authority"]
                )
        raise PermissionError("organization credential invalid or expired")

    def resolve_actor(self, requestor: TrustedIdentity, actor_id: str) -> TrustedIdentity | None:
        """Resolve a sharing recipient from this authority's live directory.

        The caller supplies only an actor label. Authority and execution subject
        come from configured credentials; no digest or token is exposed.
        """
        if not isinstance(requestor, TrustedIdentity) or not isinstance(actor_id, str) or not actor_id.strip():
            return None
        config = self._config()
        if requestor.authority != config["authority"]:
            return None
        for entry in config["credentials"]:
            if entry.get("actor_id") != actor_id:
                continue
            expiry = float(entry["expires_at"])
            if not entry.get("revoked", False) and math.isfinite(expiry) and expiry > time.time():
                return TrustedIdentity(actor_id, actor_id, config["authority"])
        return None

    def known_actor(self, identity: TrustedIdentity) -> bool:
        """Recognize a durable subject, independently of its current login.

        This is a directory lookup, never request authentication. Expiring or
        signing out one credential does not erase a persistent share's owner.
        """
        if not isinstance(identity, TrustedIdentity):
            return False
        config = self._config()
        return (
            identity.authority == config["authority"]
            and identity.subject_id == identity.actor_id
            and any(entry.get("actor_id") == identity.actor_id for entry in config["credentials"])
        )

    def principal(self, headers: Any) -> AuthenticatedPrincipal:
        lower = {str(k).lower(): str(v) for k, v in headers.items()}
        authorization = lower.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token:
            cookie = SimpleCookie()
            try:
                cookie.load(lower.get("cookie", ""))
            except CookieError as error:
                raise PermissionError(
                    "invalid organization credential cookie"
                ) from error
            token = cookie[COOKIE].value if COOKIE in cookie else ""
        if len(token) < 32 or len(token) > 4096:
            raise PermissionError("organization credential required")
        digest = hashlib.sha256(token.encode()).hexdigest()
        return AuthenticatedPrincipal(
            str(self.path), digest, self.resolve_digest(digest)
        )

    async def authenticate(self, context: AuthContext) -> AuthResult:
        try:
            principal = self.principal(context.headers)
            return AuthResult(True, principal.identity().actor_id)
        except (OSError, ValueError, KeyError, TypeError, PermissionError):
            return AuthResult(False, error="organization authentication failed")

    @staticmethod
    def _canonical(value: dict) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()

    def sign(self, payload: dict) -> dict:
        wire = dict(payload)
        wire.pop(ASSERTION, None)
        principal = _current.get()
        if principal is not None:
            if principal.config_path != str(self.path):
                raise PermissionError("organization authentication authority mismatch")
            principal.identity()
        claims = {
            "aud": "jiuwenswarm-agentserver",
            "iat": time.time(),
            "exp": time.time() + 15,
            "nonce": secrets.token_hex(24),
            "credential": principal.credential_digest if principal else None,
            "actor": principal.bound_identity.actor_id if principal else None,
            "authority": principal.bound_identity.authority if principal else None,
            "body": hashlib.sha256(self._canonical(wire)).hexdigest(),
        }
        config = self._config()
        signature = hmac.new(
            bytes.fromhex(config["signing_key"]),
            self._canonical(claims),
            hashlib.sha256,
        ).hexdigest()
        wire[ASSERTION] = {"claims": claims, "signature": signature}
        return wire

    def verify(self, payload: dict) -> AuthenticatedPrincipal | None:
        wire = dict(payload)
        assertion = wire.pop(ASSERTION, None)
        if not isinstance(assertion, dict):
            raise PermissionError("authenticated Gateway assertion required")
        claims = assertion["claims"]
        config = self._config()
        expected = hmac.new(
            bytes.fromhex(config["signing_key"]),
            self._canonical(claims),
            hashlib.sha256,
        ).hexdigest()
        now = time.time()
        if (
            not hmac.compare_digest(expected, str(assertion.get("signature", "")))
            or claims.get("aud") != "jiuwenswarm-agentserver"
            or not self._started_at <= float(claims["iat"]) <= now
            or not now < float(claims["exp"]) <= now + 20
            or claims.get("body") != hashlib.sha256(self._canonical(wire)).hexdigest()
        ):
            raise PermissionError("invalid Gateway assertion")
        self._seen = {
            nonce: expiry for nonce, expiry in self._seen.items() if expiry > now
        }
        nonce = claims["nonce"]
        if (
            not isinstance(nonce, str)
            or len(nonce) != 48
            or nonce in self._seen
            or len(self._seen) >= 10000
        ):
            raise PermissionError("replayed or overloaded Gateway assertion")
        self._seen[nonce] = float(claims["exp"])
        digest = claims["credential"]
        if digest is None:
            # Authenticated service work has no human identity. It must never
            # acquire the legacy local-installation identity in protected mode.
            return None
        identity = self.resolve_digest(digest)
        if identity.actor_id != claims.get("actor") or identity.authority != claims.get(
            "authority"
        ):
            raise PermissionError("organization identity changed")
        return AuthenticatedPrincipal(str(self.path), digest, identity)


_instances: dict[str, OrganizationAuthenticator] = {}


def configured_authenticator() -> OrganizationAuthenticator | None:
    path = os.environ.get(CONFIG_ENV, "")
    if not path:
        return None
    resolved = str(Path(path).resolve())
    if resolved not in _instances:
        _instances[resolved] = OrganizationAuthenticator(resolved)
    return _instances[resolved]


def current_principal() -> AuthenticatedPrincipal | None:
    """Capture an opaque host principal for an existing asynchronous queue."""
    principal = _current.get()
    if principal is not None:
        principal.identity()
    return principal


def current_identity() -> TrustedIdentity | None:
    principal = _current.get()
    return principal.identity() if principal is not None else None


@contextmanager
def authenticated_scope(principal: AuthenticatedPrincipal | None):
    token = _current.set(principal)
    try:
        yield
    finally:
        _current.reset(token)


def connection_principal(ws: Any) -> AuthenticatedPrincipal | None:
    auth = configured_authenticator()
    if auth is None:
        return None
    cached = getattr(ws, "_jiuwen_organization_principal", None)
    if cached is not None:
        if not isinstance(cached, AuthenticatedPrincipal) or cached.config_path != str(
            auth.path
        ):
            raise PermissionError("organization authority changed")
        cached.identity()
        return cached
    headers = getattr(ws, "request_headers", None)
    if headers is None:
        headers = getattr(getattr(ws, "request", None), "headers", {})
    principal = auth.principal(headers)
    setattr(ws, "_jiuwen_organization_principal", principal)
    return principal
