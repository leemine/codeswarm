"""Credential-free creation choices; catalog presence never grants execution.

This projection does not create a workspace, Binding, Session or Provider.
The ordinary session.create and runtime admission paths remain authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping

from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_providers.construction import compile_execution

from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.surface import canonical_surface_mode
from jiuwenswarm.runtime.model_catalog import build_model_catalog


def parse_execution_options(params):
    if type(params) is not dict or set(params) - {"mode", "work_mode"}:
        raise ValueError("invalid execution options fields")
    if params.get("work_mode") not in ("work", "code"):
        raise ValueError("work_mode is required")
    if not isinstance(params.get("mode"), str):
        raise ValueError("mode is required")
    return canonical_surface_mode(params)


def execution_options(config: Mapping, entries, params, *, governed: bool):
    mode = parse_execution_options(params)
    catalog = load_execution_catalog(config)
    models = build_model_catalog(entries).models
    profiles = catalog.profile_ids if catalog else (None,)
    options = []
    for profile_id in profiles:
        spec = catalog.source(explicit_profile_id=profile_id).resolve() if catalog else None
        provider = spec.provider_id if spec else "native"
        reason = None
        model_keys = None  # None preserves the original Native model picker.
        if mode.startswith("team.") or mode.endswith(".plan"):
            reason = "mode_unavailable" if provider != "native" or governed and mode.startswith("team.") else None
        elif provider == "native":
            if spec is not None:
                try:
                    compile_execution(spec)
                except Exception:
                    reason = "configuration_unavailable"
        elif provider == "codex" and not governed:
            # Preserve the already supported personal Codex creation path.
            # Organization mandatory-tool admission remains unavailable.
            try:
                from openjiuwen.harness_providers.codex import CodexHarnessConfig
                CodexHarnessConfig.from_mapping(compile_execution(spec))
            except Exception:
                reason = "configuration_unavailable"
        elif provider == "opencode":
            try:
                from openjiuwen.harness_providers.opencode import OpenCodeHarnessConfig
                configured = OpenCodeHarnessConfig.from_mapping(compile_execution(spec))
                model_keys = []
                if configured.model is not None:
                    for model in models:
                        index = int(model.selection_key.rpartition("#")[2])
                        client = entries[index].get("model_client_config", {})
                        if (client.get("model_name") != configured.model.model
                                or client.get("api_base") != configured.model.api_base
                                or model.is_agentos):
                            continue
                        if governed:
                            # Reuse the delivered Single eligibility rule. It
                            # inspects metadata only, without reading a secret.
                            from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
                            from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
                            binding = ModelCredentialBinding.from_config(client)
                            ContinuationTargets._execution(spec, binding)
                        model_keys.append(model.selection_key)
                if not governed:
                    # Personal profiles may own their model configuration (or
                    # use the CLI default), as before this picker existed.
                    model_keys = None
                elif not model_keys:
                    reason = "model_unavailable"
            except Exception:
                reason = "configuration_unavailable"
        else:
            # Other Providers retain their existing entry points. This first
            # ordinary picker releases only the qualified Single combinations.
            reason = "provider_unavailable"
        options.append({
            "execution_profile_id": profile_id,
            "provider_id": provider,
            "config_fingerprint": config_fingerprint(spec) if spec else "legacy-native",
            "available": reason is None,
            "reason": reason,
            "model_selection_keys": model_keys,
        })
    return {"options": options, "default_profile_id": catalog.default_profile_id if catalog else None}


def execution_display(metadata, config):
    """Never relabel a historical Binding from a changed default/profile."""
    profile_id = metadata.get("execution_profile_id")
    if not profile_id:
        return {"execution_profile_id": None, "provider_id": "native"}
    provider = None
    try:
        catalog = load_execution_catalog(config)
        spec = catalog.source(explicit_profile_id=profile_id).resolve()
        if (spec.config_revision == metadata.get("execution_config_revision")
                and config_fingerprint(spec) == metadata.get("execution_config_fingerprint")):
            provider = spec.provider_id
    except Exception:
        pass
    return {"execution_profile_id": profile_id, "provider_id": provider}
