# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read-only UI projection; never admit, cache or construct an execution."""
from pathlib import Path

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import RuntimeSurface
from openjiuwen.harness_providers.construction import configured_provider_capabilities, execution_authorization

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.capability_catalog import compile_capability_catalog
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog, source_for_bound_fingerprint
from jiuwenswarm.runtime.harness.external_subagent_profiles import surface_external_subagent_profiles
from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot, SurfaceAdmissionError, build_surface_identity,
    compile_surface_policy, validate_surface_metadata,
)
from jiuwenswarm.runtime.harness.ui_capability_manifest import (
    compile_native_ui_capability_manifest, compile_ui_capability_manifest,
)


def cold_surface_manifest(metadata, *, config, channel_id, session_id, browser_available):
    """Project fixed catalog metadata without registering an execution Binding.

    The temporary value objects below only supply the existing capability
    compiler's shape. They are not stored, admitted or passed to a Provider.
    No projectless Workspace is allocated and no recovery/context is loaded.
    """
    catalog = load_execution_catalog(config)
    if catalog is None:
        raise SurfaceAdmissionError('execution profile is no longer configured')
    source = catalog.source(explicit_profile_id=metadata['execution_profile_id'])
    if source.resolve().config_revision != metadata.get('execution_config_revision'):
        raise SurfaceAdmissionError('execution configuration changed')
    try:
        spec = source_for_bound_fingerprint(
            source, metadata.get('execution_config_fingerprint'),
            allow_runtime_authorization=not str(metadata.get('mode', '')).startswith('team'),
        ).resolve()
    except ValueError as exc:
        raise SurfaceAdmissionError('execution configuration changed') from exc
    mode = validate_surface_metadata(metadata)
    surface = RuntimeSurface(mode.split('.')[1])
    if spec.provider_id == 'native':
        return compile_native_ui_capability_manifest(surface)
    root = Path(metadata.get('project_dir') or '.').expanduser().resolve()
    paths = RuntimeWorkspacePaths(root, root, root, root)
    binding = ExecutionBinding.create(spec, subject_id=metadata.get('user_id') or f'{channel_id}:{session_id}',
                                      host_session_id=session_id, workspace=str(root))
    snapshot = EffectiveSurfaceSnapshot(build_surface_identity(
        metadata=metadata, binding=binding, paths=paths, channel_id=channel_id), mode)
    authorization = execution_authorization(spec)
    snapshot = compile_surface_policy(snapshot, authorization=authorization,
        include_personal_context=False, topology=snapshot.identity.topology)
    profiles = surface_external_subagent_profiles(surface.value, browser_available=browser_available)
    # UI projects categories, not individual product tools. The same trusted
    # profile catalog determines the optional browser/subagent categories.
    capabilities = compile_capability_catalog(snapshot,
        provider_inventory=configured_provider_capabilities(spec), product_tool_names=(),
        product_subagent_types=tuple(profile.subagent_type for profile in profiles),
        authorization=authorization)
    return compile_ui_capability_manifest(capabilities)
