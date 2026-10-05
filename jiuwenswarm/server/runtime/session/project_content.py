"""Project Instructions/Sources versions in the existing ACL sidecar authority."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.project_content import (
    ProjectContentSnapshot,
    compile_project_content,
)
from jiuwenswarm.server.runtime.session.project_access import (
    ProjectAccessDenied,
    ProjectAccessStore,
    ProjectRevisionConflict,
)

_MAX_INSTRUCTIONS = 64 * 1024
_MAX_SOURCE = 128 * 1024
_MAX_TOTAL = 1024 * 1024
_MAX_SOURCES = 32


class ProjectContentStore:
    def __init__(self, access_store: ProjectAccessStore | None = None):
        self.access = access_store if access_store is not None else ProjectAccessStore()

    @staticmethod
    def _actor(identity: TrustedIdentity) -> str:
        if not isinstance(identity, TrustedIdentity):
            raise ProjectAccessDenied("trusted project content identity required")
        return identity.actor_id

    def _record(self, data: dict, project_id: str) -> dict:
        record = data["projects"].get(project_id)
        if not isinstance(record, dict) or self.access._record(project_id) is None:
            raise ProjectAccessDenied("managed project required for project content")
        return record

    @staticmethod
    def _state(record: dict) -> dict:
        state = record.get("project_content")
        if state is None:
            return {"schema_version": 1, "revision": 0, "versions": {}}
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or type(state.get("revision")) is not int
            or state["revision"] < 1
            or not isinstance(state.get("versions"), dict)
            or str(state["revision"]) not in state["versions"]
        ):
            raise ProjectAccessDenied("project content storage unavailable")
        return state

    @staticmethod
    def _version(state: dict, revision: int | None = None) -> dict:
        revision = state["revision"] if revision is None else revision
        if type(revision) is not int or revision < 0:
            raise ValueError("project content revision must be a nonnegative integer")
        if revision == 0 and state["revision"] == 0:
            return {"revision": 0, "instructions": "", "sources": []}
        version = state["versions"].get(str(revision))
        if not isinstance(version, dict) or version.get("revision") != revision:
            raise ValueError("project content revision unavailable")
        return version

    def get(
        self, project_id: str, identity: TrustedIdentity, *, revision: int | None = None
    ) -> dict:
        actor = self._actor(identity)
        with self.access.guard(project_id, actor, "read"):
            data = self.access._load()
            state = self._state(self._record(data, project_id))
            version = self._version(state, revision)
            # Validate persisted content before exposing it to a client.
            compile_project_content(project_id, version)
            return {
                "project_id": project_id,
                **copy.deepcopy(version),
                "latest_revision": state["revision"],
                "can_write": self.access._decision(
                    data, project_id, actor, "write"
                ).allowed,
                "versions": [
                    {"revision": item["revision"], "updated_at": item["updated_at"]}
                    for item in reversed(list(state["versions"].values()))
                ],
            }

    @staticmethod
    def _text(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
            raise ValueError(f"{field} must be text within {limit} UTF-8 bytes")
        return value

    def update(
        self,
        project_id: str,
        identity: TrustedIdentity,
        *,
        instructions: str,
        sources: list[dict],
        expected_revision: int,
    ) -> dict:
        actor = self._actor(identity)
        # Check authorization before parsing or looking up protected source content.
        with self.access.guard(project_id, actor, "write"):
            data = self.access._load()
            record = self._record(data, project_id)
            state = self._state(record)
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("expected content revision is required")
            if expected_revision != state["revision"]:
                raise ProjectRevisionConflict(
                    "project content revision changed; reload before saving"
                )
            instructions = self._text(instructions, "instructions", _MAX_INSTRUCTIONS)
            if not isinstance(sources, list) or len(sources) > _MAX_SOURCES:
                raise ValueError(
                    f"sources must be a list of at most {_MAX_SOURCES} entries"
                )
            previous = {
                source["source_id"]: source
                for source in self._version(state)["sources"]
            }
            source_revisions = {}
            for version in state["versions"].values():
                for source in version["sources"]:
                    source_revisions[source["source_id"]] = max(
                        source_revisions.get(source["source_id"], 0), source["revision"]
                    )
            compiled_sources, seen = [], set()
            total_bytes = len(instructions.encode("utf-8"))
            for source in sources:
                if not isinstance(source, dict) or set(source) - {
                    "source_id",
                    "title",
                    "origin",
                    "content",
                    "trust",
                    "revision",
                }:
                    raise ValueError("unknown project source fields")
                source_id = source.get("source_id") or uuid.uuid4().hex
                if (
                    not isinstance(source_id, str)
                    or not source_id.strip()
                    or source_id != source_id.strip()
                    or len(source_id) > 128
                    or source_id in seen
                ):
                    raise ValueError("source IDs must be unique normalized strings")
                seen.add(source_id)
                if source.get("trust", "untrusted") != "untrusted":
                    raise ValueError(
                        "project sources must remain untrusted reference data"
                    )
                content = {
                    "source_id": source_id,
                    "title": self._text(source.get("title", ""), "source title", 1024),
                    "origin": self._text(
                        source.get("origin", ""), "source origin", 4096
                    ),
                    "content": self._text(
                        source.get("content"), "source content", _MAX_SOURCE
                    ),
                    "trust": "untrusted",
                }
                old = previous.get(source_id)
                revision = (
                    old["revision"]
                    if old is not None
                    and all(old.get(key) == value for key, value in content.items())
                    else source_revisions.get(source_id, 0) + 1
                )
                compiled_sources.append({**content, "revision": revision})
                total_bytes += sum(
                    len(value.encode("utf-8")) for value in content.values()
                )
            if total_bytes > _MAX_TOTAL:
                raise ValueError("project content exceeds the total UTF-8 byte limit")
            revision = state["revision"] + 1
            version = {
                "revision": revision,
                "instructions": instructions,
                "sources": compiled_sources,
                "updated_at": time.time(),
                "updated_by": actor,
            }
            compile_project_content(project_id, version)
            state["versions"][str(revision)] = version
            state["revision"] = revision
            record["project_content"] = state
            self.access._save(data)
            return {
                "project_id": project_id,
                **copy.deepcopy(version),
                "latest_revision": revision,
                "can_write": True,
                "versions": [
                    {"revision": item["revision"], "updated_at": item["updated_at"]}
                    for item in reversed(list(state["versions"].values()))
                ],
            }

    def freeze(
        self, project_id: str, identity: TrustedIdentity
    ) -> ProjectContentSnapshot:
        actor = self._actor(identity)
        with self.access.guard(project_id, actor, "read"):
            with self.access.guard(project_id, actor, "execute"):
                state = self._state(self._record(self.access._load(), project_id))
                return compile_project_content(project_id, self._version(state))
