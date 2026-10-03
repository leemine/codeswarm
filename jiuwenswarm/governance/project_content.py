"""Pure immutable project content compilation; Sources never become rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

COMPILE_VERSION = "project-content/v1"


@dataclass(frozen=True, slots=True)
class ProjectSource:
    source_id: str
    revision: int
    title: str
    origin: str
    content: str
    trust: str = "untrusted"

    def __post_init__(self) -> None:
        if not self.source_id or type(self.revision) is not int or self.revision < 1:
            raise ValueError("invalid project source identity or revision")
        if self.trust != "untrusted":
            raise ValueError("project sources must remain untrusted reference data")
        if any(
            not isinstance(value, str)
            for value in (self.source_id, self.title, self.origin, self.content)
        ):
            raise ValueError("project source fields must be strings")


@dataclass(frozen=True, slots=True)
class ProjectContentSnapshot:
    project_id: str
    revision: int
    instructions: str
    sources: tuple[ProjectSource, ...]
    compile_version: str
    digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sources, tuple)
            or any(not isinstance(source, ProjectSource) for source in self.sources)
            or not isinstance(self.instructions, str)
            or type(self.revision) is not int
            or self.revision < 0
            or self.compile_version != COMPILE_VERSION
            or not isinstance(self.digest, str)
            or len(self.digest) != 64
        ):
            raise ValueError("invalid immutable project content snapshot")

    @property
    def reference_json(self) -> str:
        """A structured data payload, separate from instruction authority.

        JSON escaping preserves the source boundary even for delimiter strings
        and control characters. Hosts must retain the untrusted-data role.
        """
        return json.dumps(
            {
                "kind": "project_reference_data",
                "trust": "untrusted",
                "sources": [asdict(source) for source in self.sources],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def compile_project_content(
    project_id: str, content: Mapping[str, Any]
) -> ProjectContentSnapshot:
    """Compile an already authorized version without IO, mutable aliases or time.

    Hosts should normally call ProjectContentStore.freeze(), which authorizes
    before materializing the document and calls this pure function once per Turn.
    """
    revision = content.get("revision")
    instructions = content.get("instructions")
    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or type(revision) is not int
        or revision < 0
        or not isinstance(instructions, str)
    ):
        raise ValueError("invalid project content snapshot")
    raw_sources = content.get("sources")
    if not isinstance(raw_sources, (list, tuple)):
        raise ValueError("project sources must be a sequence")
    sources = tuple(ProjectSource(**dict(source)) for source in raw_sources)
    if len({source.source_id for source in sources}) != len(sources):
        raise ValueError("duplicate project source identity")
    canonical = {
        "project_id": project_id,
        "revision": revision,
        "instructions": instructions,
        "sources": [asdict(source) for source in sources],
        "compile_version": COMPILE_VERSION,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ProjectContentSnapshot(
        project_id,
        revision,
        instructions,
        sources,
        COMPILE_VERSION,
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )
