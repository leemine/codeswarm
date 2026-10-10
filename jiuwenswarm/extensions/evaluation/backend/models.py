"""Strict, credential-free business DTOs; no Provider event or state duplication."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")]


def canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 240
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
        or ":" in value
        or any(part in {".git", ".evaluation"} for part in path.parts)
    ):
        raise ValueError("expected a normalized relative workspace path")
    return value


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1


class InputFile(DTO):
    path: str
    content: str = Field(max_length=262144)
    executable: bool = False

    _path = field_validator("path")(relative_path)


class Acceptance(DTO):
    kind: Literal["manual", "python"] = "manual"
    script: str = Field(default="", max_length=65536)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    dependency_lock: str | None = None

    @field_validator("dependency_lock")
    @classmethod
    def lock_path(cls, value):
        return relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def executable(self):
        if (self.kind == "python") != bool(self.script.strip()):
            raise ValueError(
                "python acceptance requires a script; manual acceptance has no script"
            )
        return self


class TaskDraft(DTO):
    task_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=65536)
    files: tuple[InputFile, ...] = ()
    deliverables: tuple[str, ...] = ()
    acceptance: Acceptance = Field(default_factory=Acceptance)
    environment: Literal["shared-host-v1"] = "shared-host-v1"

    @field_validator("name", "instruction")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def paths(self):
        paths = [item.path for item in self.files]
        if len(paths) > 100 or len(set(paths)) != len(paths):
            raise ValueError("duplicate paths or too many files")
        if sum(len(item.content.encode()) for item in self.files) > 1048576:
            raise ValueError("input materials exceed 1 MiB")
        for path in paths:
            if any(path.startswith(other + "/") for other in paths if other != path):
                raise ValueError("file/directory path conflict")
        if len(self.deliverables) > 100 or len(set(self.deliverables)) != len(
            self.deliverables
        ):
            raise ValueError("duplicate or too many deliverables")
        for path in self.deliverables:
            relative_path(path)
        if self.acceptance.dependency_lock and self.acceptance.dependency_lock not in {
            *paths,
            *self.deliverables,
        }:
            raise ValueError(
                "dependency lock must be in the baseline or declared deliveries"
            )
        return self


class TaskRef(DTO):
    task_id: Identifier
    revision: int = Field(ge=1)


class DatasetDraft(DTO):
    dataset_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    tasks: tuple[TaskRef, ...] = Field(min_length=1, max_length=100)
    source: str = Field(default="manual", max_length=1024)
    license: str = Field(default="user-provided", max_length=1024)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("duplicate task reference")
        return self


class ExecutionPlan(DTO):
    model: str = Field(min_length=1, max_length=256)
    execution_profile_id: str = Field(min_length=1, max_length=128)
    provider_id: Literal["native", "opencode"] = "native"


class ExperimentDraft(DTO):
    name: str = Field(min_length=1, max_length=160)
    tasks: tuple[TaskRef, ...] = Field(min_length=1, max_length=100)
    model: str = Field(default="", max_length=256)
    execution_profile_id: str = Field(default="", max_length=128)
    provider_id: Literal["native", "opencode"] = "native"
    plans: tuple[ExecutionPlan, ...] = Field(default=(), max_length=8)
    repeats: int = Field(default=1, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=4)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    acceptance_policy: Literal["shared-environment-v1", "independent-container-v1"] = (
        "shared-environment-v1"
    )
    shared_environment_acknowledged: Literal[True]
    dataset_id: Identifier | None = None
    dataset_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def references(self):
        if self.plans:
            if self.model or self.execution_profile_id or self.provider_id != "native":
                raise ValueError("use plans or legacy single-plan fields, not both")
            if len(set(self.plans)) != len(self.plans):
                raise ValueError("duplicate execution plan; use independent repeats")
        elif not self.model.strip() or not self.execution_profile_id.strip():
            raise ValueError("model and execution profile are required")
        if len(self.tasks) * len(self.execution_plans) * self.repeats > 500:
            raise ValueError("experiment exceeds 500 trials")
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("duplicate task reference")
        if (self.dataset_id is None) != (self.dataset_revision is None):
            raise ValueError("dataset ID and revision must be specified together")
        return self

    @property
    def execution_plans(self):
        return self.plans or (ExecutionPlan(
            model=self.model, execution_profile_id=self.execution_profile_id,
            provider_id=self.provider_id,
        ),)

    def for_plan(self, index):
        plan = self.execution_plans[index]
        return self.model_copy(update={
            "plans": (), "model": plan.model,
            "execution_profile_id": plan.execution_profile_id,
            "provider_id": plan.provider_id,
        })


def decode(model, value):
    """JSON decoding permits JSON arrays for immutable tuples, with strict scalars."""
    return model.model_validate_json(canonical(value))
