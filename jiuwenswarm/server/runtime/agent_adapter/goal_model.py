# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve a Goal assessor from the product's configured model selection."""
from __future__ import annotations

from copy import deepcopy

from jiuwenswarm.common.config import get_default_models
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.model_catalog import ModelCatalogError
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata


def request_goal_assessor_factory(request):
    # Selectors may come from the request; connection settings and credentials
    # only come from the server configuration. Login models require their own
    # authenticated factory, never raw client-supplied credential fields.
    params = request.params if isinstance(request.params, dict) else {}
    if E2A_MODEL_AUTH_PARAM_KEY in params:
        raise ModelCatalogError(
            "Goal assessment requires an authorized model factory for login models",
            code="GOAL_ASSESSOR_AUTHORIZATION_REQUIRED",
        )
    runtime = get_current_runtime()
    if runtime is None:
        raise ModelCatalogError("Goal model catalog is unavailable", code="MODEL_CATALOG_UNAVAILABLE")
    requested = str(params.get("model_name") or "").strip()
    if not requested:
        metadata = get_session_metadata(request.session_id, enable_writeback=False) or {}
        requested = str(metadata.get("model") or "").strip()
    if not requested:
        requested = runtime.list_model_capabilities().current_selection
    selected = runtime.resolve_model_capability(requested)
    entries = deepcopy(get_default_models())
    from jiuwenswarm.runtime.harness.goal_assessment import catalog_model_factory

    return catalog_model_factory(
        entries, selected.selection_key,
        requires_request_authorization=selected.is_agentos,
    )
