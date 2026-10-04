"""Static B3 target facts; never create a Session, Model, credential or grant.

The initial combination is Single Native normal with an empty provider config.
These facts do not authorize tools/processes or prove actual model consumption.
Runtime must revalidate before publication and enforce all execution boundaries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openjiuwen.harness.engine.config import config_fingerprint

from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.contracts import AuthorizationDecision, TrustedIdentity
from jiuwenswarm.governance.model_credentials import (
    ModelCredentialBinding,
    configured_model_metadata,
)
from jiuwenswarm.governance.resources import (
    ResourceAccessDenied,
    ResourceDecision,
    ResourceGuard,
    ResourceRequest,
)
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.surface import canonical_surface_mode
from jiuwenswarm.runtime.model_catalog import (
    build_model_catalog,
    resolve_model_selection,
)
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput


@dataclass(frozen=True, slots=True, repr=False)
class ContinuationTarget:
    """Private immutable selection facts, not a wire authorization receipt."""

    request: ContinuationInput
    identity: TrustedIdentity
    provision_input: SessionCreateInput
    execution_fingerprint: str
    execution_revision: str
    model_binding: ModelCredentialBinding
    model_binding_fingerprint: str
    model_entry_fingerprint: str
    project_acl_revision: int
    resource_requests: tuple[ResourceRequest, ...]
    resource_decisions: tuple[ResourceDecision, ...]
    resource_revision: int
    _selector: object = field(repr=False, compare=False)


def _checksum(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _metadata_only(value):
    """Reject unresolved or secret-bearing configuration without echoing it."""
    if isinstance(value, dict):
        forbidden = {
            "api_key",
            "secret",
            "password",
            "token",
            "authorization",
            "custom_headers",
            "headers",
            "credentials",
            "env",
        }
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in forbidden:
                raise ValueError("unsupported model metadata")
            _metadata_only(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _metadata_only(item)
    elif isinstance(value, str):
        if "${" in value:
            raise ValueError("unresolved model metadata")
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError("unsupported model metadata")


class ContinuationTargets:
    """Read host catalogs and current managed authority; keep no mutable approval."""

    def __init__(
        self,
        runtime,
        *,
        catalog_source=None,
        model_entries_source=None,
        project_store=None,
    ):
        if catalog_source is None:
            from jiuwenswarm.common.config import get_config_raw

            catalog_source = get_config_raw
        if project_store is None:
            from jiuwenswarm.server.runtime.session import project_store
        self._marker = object()
        self._runtime = runtime
        self._catalog_source = catalog_source
        self._model_entries_source = model_entries_source or configured_model_metadata
        self._projects = project_store

    def _project_decision(self, project_id, identity, action):
        decision = self._runtime._submission_guard.check_access(
            project_id, identity, action
        )
        if not isinstance(decision, AuthorizationDecision):
            raise ValueError("managed project authority required")
        decision.__post_init__()
        if (
            decision.allowed is not True
            or decision.revision < 1
            or (decision.project_id, decision.actor_id, decision.action)
            != (project_id, identity.actor_id, action)
        ):
            raise ValueError("managed project authority required")
        return decision

    def _project(self, project_id):
        project = self._projects.get_project_by_id(project_id, cache_bust=True)
        if (
            project is None
            or project.project_id != project_id
            or project.work_mode not in ("work", "code")
            or not isinstance(project.project_dir, str)
            or not project.project_dir
            or "${" in project.project_dir
            or not Path(project.project_dir).is_absolute()
        ):
            raise ValueError("managed project unavailable")
        return str(Path(project.project_dir).resolve(strict=True)), project.work_mode

    def _model(self, request):
        if not request.model_name:
            raise ValueError("explicit model selection required")
        entries = tuple(self._model_entries_source())
        selected = resolve_model_selection(
            build_model_catalog(entries), request.model_name
        )
        if selected.selection_key != request.model_name or selected.is_agentos:
            raise ValueError("exact configured model selection required")
        index = int(selected.selection_key.rpartition("#")[2])
        entry = entries[index]
        config = entry["model_client_config"]
        if config.get("model_name") != selected.model_name:
            raise ValueError("model selection mismatch")
        binding = ModelCredentialBinding.from_config(config)
        request_config = entry.get("model_config_obj") or {}
        if not isinstance(request_config, dict):
            raise ValueError("invalid model request config")
        for key in ("model", "model_name"):
            if key in request_config and request_config[key] != binding.model:
                raise ValueError("model request binding mismatch")
        # The real credential resolver also requires one exact catalog binding.
        matches = 0
        for candidate in entries:
            try:
                matches += (
                    ModelCredentialBinding.from_config(
                        candidate.get("model_client_config")
                    )
                    == binding
                )
            except (AttributeError, TypeError, ValueError, ResourceAccessDenied):
                continue
        if matches != 1:
            raise ValueError("ambiguous model binding")
        # Never store/read the api_key value or raw entry in a target. Include
        # all retained request settings so an unchanged name cannot hide drift.
        metadata = {
            "client": {key: value for key, value in config.items() if key != "api_key"},
            "request": request_config,
        }
        _metadata_only(metadata)
        return binding, _checksum(asdict(binding)), _checksum(metadata)

    def select(self, request: ContinuationInput) -> ContinuationTarget:
        try:
            return self._select(request)
        except Exception:
            # Third-party sources and validators may include secrets in errors.
            raise ResourceAccessDenied("continuation target unavailable") from None

    def _select(self, request):
        if type(request) is not ContinuationInput:
            raise ValueError("continuation input required")
        request.__post_init__()
        identity = self._runtime._governance_identity(request)
        if type(identity) is not TrustedIdentity:
            raise ValueError("trusted identity required")
        identity.__post_init__()
        pid = request.target_project_id
        read = self._project_decision(pid, identity, "read")
        execute = self._project_decision(pid, identity, "execute")
        if read.revision != execute.revision:
            raise ValueError("project authority changed")
        root, work_mode = self._project(pid)
        if not Path(root).is_dir():
            raise ValueError("project workspace unavailable")
        mode = canonical_surface_mode({"mode": request.mode, "work_mode": work_mode})
        if mode not in ("agent.work.normal", "agent.code.normal"):
            raise ValueError("unsupported continuation surface")
        config = self._catalog_source()
        permissions = config.get("permissions")
        if permissions is not None and (
            not isinstance(permissions, dict)
            or "enabled" in permissions
            and (
                type(permissions["enabled"]) is not bool
                or permissions["enabled"] is False
            )
        ):
            raise ValueError("unresolved permission configuration")
        catalog = load_execution_catalog(config)
        if catalog is None:
            raise ValueError("explicit execution catalog required")
        spec = catalog.source(
            explicit_profile_id=request.execution_profile_id
        ).resolve()
        if (
            spec.provider_id != "native"
            or spec.provider_config
            or spec.requested_mode not in (None, "normal")
            or "${" in spec.config_revision
            or spec.authorization is not None
            and spec.authorization.full_access
        ):
            raise ValueError("unsupported continuation execution configuration")
        binding, binding_fingerprint, entry_fingerprint = self._model(request)
        authority = self._runtime._resource_authorizer
        grants = authority.resource_grants(pid, identity)
        revision = grants.get("resource_revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("resource inventory revision required")
        definitions = grants.get("resources")
        if not isinstance(definitions, (list, tuple)):
            raise ValueError("resource inventory unavailable")
        checks = (("workspace", "read", root), ("credential", "use", binding.reference))
        requests, decisions = [], []
        guard = ResourceGuard(authority)
        for kind, action, reference in checks:
            matches = [
                item
                for item in definitions
                if isinstance(item, dict)
                and (item.get("kind"), item.get("action"), item.get("reference"))
                == (kind, action, reference)
            ]
            if len(matches) != 1:
                raise ValueError("unique required resource unavailable")
            operation = ResourceRequest(
                matches[0]["resource_id"], action, root if kind == "workspace" else None
            )
            decision = guard.check(pid, identity, operation)
            if (
                decision.resource_revision != revision
                or decision.acl_revision != read.revision
                or decision.reference != reference
                or kind == "workspace"
                and decision.scope != root
            ):
                raise ValueError("resource boundary changed")
            requests.append(operation)
            decisions.append(decision)
        # No sidecar lock spans these independent reads or any await. Reject
        # detectable drift now; Runtime revalidates again at actual publication.
        if (
            self._runtime._governance_identity(request) != identity
            or self._project(pid) != (root, work_mode)
            or self._project_decision(pid, identity, "read") != read
            or self._project_decision(pid, identity, "execute") != execute
        ):
            raise ValueError("target changed during selection")
        provision = SessionCreateInput(
            channel_id="web",
            create_token=request.create_token,
            persist_session=True,
            persist_session_supplied=True,
            mode=mode,
            project_id=pid,
            project_dir=root,
            cwd=root,
            work_mode=work_mode,
            work_mode_explicit=True,
            title=request.title,
            model_name=request.model_name,
            execution_profile_id=request.execution_profile_id,
        )
        return ContinuationTarget(
            request,
            identity,
            provision,
            config_fingerprint(spec),
            spec.config_revision,
            binding,
            binding_fingerprint,
            entry_fingerprint,
            read.revision,
            tuple(requests),
            tuple(decisions),
            revision,
            self._marker,
        )

    def revalidate(self, target: ContinuationTarget) -> ContinuationTarget:
        if (
            type(target) is not ContinuationTarget
            or target._selector is not self._marker
        ):
            raise ResourceAccessDenied("continuation target unavailable")
        current = self.select(target.request)
        if current != target:
            raise ResourceAccessDenied("continuation target changed")
        return target
