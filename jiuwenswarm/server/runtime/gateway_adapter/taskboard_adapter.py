"""Taskboard RPCs execute only in their owning AgentServer instance."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.extensions.taskboard.extension import taskboard_enabled
from jiuwenswarm.extensions.taskboard.backend.models import TaskboardError
from jiuwenswarm.extensions.taskboard.backend.store import TaskboardStore
from jiuwenswarm.extensions.taskboard.backend.service import TaskboardService
from jiuwenswarm.server.runtime.session.project_access import (
    ProjectAccessStore,
    ProjectAccessDenied,
)
from .base import GatewayAdapter, build_error_response
from .project_adapter import readable_reference_snapshot

logger = logging.getLogger(__name__)


class TaskboardAdapter(GatewayAdapter):
    methods = frozenset(
        {"taskboard.create", "taskboard.list", "taskboard.get", "taskboard.update"}
    )

    def __init__(self, identity_resolver=None, session_host=None):
        self.identity_resolver = identity_resolver
        self.session_host = session_host

    async def handle(self, request):
        if not taskboard_enabled():
            return build_error_response(
                request, "Taskboard disabled", code="FEATURE_DISABLED"
            )
        try:
            identity = (
                self.identity_resolver(request) if self.identity_resolver else None
            )
            if identity is not None and not isinstance(identity, TrustedIdentity):
                raise TaskboardError("FORBIDDEN", "trusted identity required")
            if self.session_host is not None and identity is None:
                raise TaskboardError("FORBIDDEN", "trusted identity required")
            owner = (
                json.dumps(asdict(identity), sort_keys=True) if identity else "local"
            )
            method = getattr(request.req_method, "value", request.req_method)
            params = request.params
            if not isinstance(params, dict):
                raise TaskboardError("BAD_REQUEST", "parameters must be an object")

            def invoke():
                # Hold the same read-authority lock through reference validation
                # and task mutation; a concurrent ACL revoke cannot race the write.
                with ProjectAccessStore()._locked():
                    projects, inventory = readable_reference_snapshot(
                        identity.actor_id if identity else ""
                    )
                    projects = {
                        p.project_id: {"name": p.name}
                        for p in projects
                        if p.work_mode == "code"
                    }
                    sessions = {}
                    for meta in inventory:
                        if (
                            meta.get("channel_id") != "web"
                            or meta.get("work_mode") != "code"
                        ):
                            continue
                        if meta.get("is_archived") or meta.get("archived"):
                            continue
                        sid = meta.get("session_id")
                        if not isinstance(sid, str):
                            continue
                        if self.session_host is not None:
                            try:
                                self.session_host.owner_revision(sid, identity)
                            except PermissionError:
                                continue
                        sessions[sid] = meta
                    service = TaskboardService(TaskboardStore(get_agent_root_dir()))
                    return service.invoke(
                        method,
                        params,
                        owner=owner,
                        projects=projects,
                        sessions=sessions,
                    )

            payload = await asyncio.to_thread(invoke)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
                metadata=request.metadata,
            )
        except TaskboardError as exc:
            return build_error_response(request, str(exc), code=exc.code)
        except (ProjectAccessDenied, PermissionError):
            return build_error_response(
                request, "reference access unavailable", code="FORBIDDEN"
            )
        except (TypeError, ValueError):
            return build_error_response(
                request, "invalid task parameters", code="BAD_REQUEST"
            )
        except Exception:
            logger.exception("Taskboard request failed")
            return build_error_response(
                request, "Taskboard storage unavailable", code="INTERNAL_ERROR"
            )
