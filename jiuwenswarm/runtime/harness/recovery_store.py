# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable, encrypted recovery material for one admitted External Session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portalocker

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import (
    CheckpointConflictError,
    CheckpointReason,
    CheckpointSaveReceipt,
    HarnessCapability,
    HarnessCard,
    HarnessCheckpoint,
    ResumePolicy,
    json_value_to_builtin,
)

from jiuwenswarm.common.auth.session_store import decrypt_json, encrypt_json
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.server.runtime.session.session_history import (
    flush_history_writes,
    get_read_history_path,
    resolve_session_dir,
    resolve_subagent_history_path,
)

_RECOVERY_SCHEMA_VERSION = 1
_RECOVERY_FILE_NAME = "execution-recovery.json"
_MAX_PENDING_INTERACTIONS = 128
_LOCK = threading.RLock()
_PROCESS_NONCE = secrets.token_hex(16)


def _process_identity() -> str:
    return f"{os.getpid()}:{_PROCESS_NONCE}"


class ExecutionRecoveryUnavailableError(RuntimeError):
    """The transcript is readable, but the original execution cannot continue."""

    code = "EXECUTION_RECOVERY_HISTORY_ONLY"
    recovery_status = "history_only"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"execution is available as read-only history: {reason}")


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryPlan:
    """Provider-cycle inputs selected from one validated recovery archive."""

    resume_policy: ResumePolicy
    checkpoint: HarnessCheckpoint | None
    checkpoint_sink: "SessionExecutionRecovery"


class SessionExecutionRecovery:
    """Persist one External Session's immutable scope and opaque checkpoint.

    The archive deliberately excludes ``AgentExecutionSpec.provider_config``:
    the server-owned catalog remains the configuration source and its digest is
    compared with the original Binding.  Provider checkpoint data is encrypted
    as one opaque value, so the host neither inspects nor stores possible
    credential-like values in plaintext.
    """

    def __init__(
        self,
        *,
        session_id: str,
        execution_profile_id: str,
        binding: ExecutionBinding,
        runtime_paths: RuntimeWorkspacePaths,
        parent_session_id: str | None = None,
        create_if_missing: bool = True,
    ) -> None:
        if parent_session_id is None:
            session_dir, error = resolve_session_dir(
                session_id,
                create=create_if_missing,
            )
            if session_dir is None:
                raise ValueError(error or "invalid session id for recovery")
            history_path = get_read_history_path(session_id)
        else:
            history_path, error = resolve_subagent_history_path(
                parent_session_id,
                session_id,
                create=create_if_missing,
            )
            if history_path is None:
                raise ValueError(error or "invalid subagent id for recovery")
            session_dir = history_path.parent
        self._session_id = session_id
        self._parent_session_id = parent_session_id
        self._profile_id = execution_profile_id
        self._binding = binding
        self._runtime_paths = runtime_paths
        self._path = session_dir / _RECOVERY_FILE_NAME
        self._history_path = history_path
        self._created = False
        if create_if_missing or self._path.exists():
            self._admit()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def created(self) -> bool:
        return self._created

    @property
    def execution_profile_id(self) -> str:
        return self._profile_id

    def child(
        self,
        binding: ExecutionBinding,
        runtime_paths: RuntimeWorkspacePaths,
        *,
        create_if_missing: bool = True,
    ) -> "SessionExecutionRecovery | None":
        """Return a child archive kept under this parent Session's storage."""

        child = SessionExecutionRecovery(
            session_id=binding.host_session_id,
            parent_session_id=self._binding.host_session_id,
            execution_profile_id=self._profile_id,
            binding=binding,
            runtime_paths=runtime_paths,
            create_if_missing=create_if_missing,
        )
        if not create_if_missing and not child.path.exists():
            return None
        return child

    def has_checkpoint(self) -> bool:
        if not self._path.exists():
            return False
        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            return isinstance(archive.get("checkpoint"), dict)

    def load_host_state(self) -> dict[str, Any]:
        """Load encrypted host-owned recovery state beside the Provider snapshot."""

        with self._archive_lock():
            archive = self._read_archive()
            encrypted = archive.get("encrypted_host_state")
            if encrypted is None:
                return {}
            try:
                state = decrypt_json(str(encrypted))
            except Exception as exc:
                raise ExecutionRecoveryUnavailableError("host recovery state is unreadable") from exc
            if not isinstance(state, dict):
                raise ExecutionRecoveryUnavailableError("host recovery state is invalid")
            return state

    def save_host_state(self, state: dict[str, Any]) -> None:
        """Synchronously checkpoint the bounded product state updated by core."""

        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            archive["encrypted_host_state"] = encrypt_json(state)
            archive["updated_at"] = time.time()
            self._write_archive(archive)

    async def mark_pending_interaction(
        self,
        request_id: str,
        *,
        turn_id: str | None,
    ) -> None:
        """Durably block cold resume while a Provider interaction is unresolved."""

        await asyncio.to_thread(
            self._mark_pending_interaction_sync,
            request_id,
            turn_id,
        )

    async def clear_pending_interactions(self) -> None:
        """Clear blockers after the interaction Turn reaches a terminal boundary."""

        await asyncio.to_thread(self._clear_pending_interactions_sync)

    def prepare(self, card: HarnessCard, *, agent_id: str) -> ExecutionRecoveryPlan:
        """Validate Provider compatibility and return its exact resume inputs."""

        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            if self._load_pending_interactions(archive):
                raise ExecutionRecoveryUnavailableError(
                    "an interaction Turn did not reach a durable terminal boundary"
                )
            persisted_card = archive.get("harness")
            if self._created and archive.get("checkpoint") is None:
                archive["harness"] = self._card_record(card)
                self._write_archive(archive)
                return ExecutionRecoveryPlan(ResumePolicy.NEW, None, self)
            if not isinstance(persisted_card, dict):
                raise ExecutionRecoveryUnavailableError("harness version is missing")
            if persisted_card != self._card_record(card):
                raise ExecutionRecoveryUnavailableError("harness version is incompatible")
            if not card.supports(HarnessCapability.CHECKPOINT):
                raise ExecutionRecoveryUnavailableError("provider does not support checkpoints")
            checkpoint = self._decode_checkpoint(archive, agent_id=agent_id, card=card)
            self._validate_replay_position(archive)
            return ExecutionRecoveryPlan(ResumePolicy.REQUIRE_RESUME, checkpoint, self)

    async def save(
        self,
        checkpoint: HarnessCheckpoint,
        *,
        reason: CheckpointReason,
        expected_storage_revision: str | None = None,
    ) -> CheckpointSaveReceipt:
        """Persist a Provider checkpoint with idempotency and compare-and-set."""

        return await asyncio.to_thread(
            self._save_sync,
            checkpoint,
            reason,
            expected_storage_revision,
        )

    def _admit(self) -> None:
        with self._archive_lock():
            if self._path.exists():
                archive = self._read_archive()
                self._validate_scope(archive)
                self._created = (
                    archive.get("checkpoint") is None
                    and archive.get("initializing_process") == _process_identity()
                )
                return
            archive = {
                "schema_version": _RECOVERY_SCHEMA_VERSION,
                "execution_profile_id": self._profile_id,
                "binding": self._binding_record(),
                "runtime_paths": self._paths_record(),
                "harness": None,
                "checkpoint": None,
                "parent_session_id": self._parent_session_id,
                "replay_position": self._capture_replay_position(),
                "created_at": time.time(),
                "updated_at": time.time(),
                "initializing_process": _process_identity(),
            }
            self._write_archive(archive)
            self._created = True

    def _save_sync(
        self,
        checkpoint: HarnessCheckpoint,
        reason: CheckpointReason,
        expected_storage_revision: str | None,
    ) -> CheckpointSaveReceipt:
        if checkpoint.host_session_id != self._binding.host_session_id:
            raise CheckpointConflictError("checkpoint host Session does not match its Binding")
        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            persisted_card = archive.get("harness")
            if not isinstance(persisted_card, dict) or checkpoint.provider != persisted_card.get("name"):
                raise CheckpointConflictError("checkpoint provider does not match its recovery archive")
            expected_agent_id = (
                f"subagent:{self._binding.host_session_id}"
                if self._parent_session_id is not None
                else f"external:{self._binding.provider_id}:{self._binding.host_session_id}"
            )
            if checkpoint.agent_id != expected_agent_id:
                raise CheckpointConflictError("checkpoint agent does not match its recovery archive")
            current = archive.get("checkpoint")
            current_revision = (
                str(current.get("storage_revision"))
                if isinstance(current, dict) and current.get("storage_revision")
                else None
            )
            if isinstance(current, dict) and current.get("checkpoint_id") == checkpoint.checkpoint_id:
                if int(current.get("sequence", -1)) != checkpoint.sequence:
                    raise CheckpointConflictError("checkpoint id was reused with a different sequence")
                try:
                    persisted_payload = decrypt_json(str(current["encrypted_payload"]))
                except Exception as exc:
                    raise CheckpointConflictError("persisted checkpoint is unreadable") from exc
                if persisted_payload != self._checkpoint_record(checkpoint):
                    raise CheckpointConflictError("checkpoint id was reused with different data")
                return CheckpointSaveReceipt(
                    checkpoint_id=checkpoint.checkpoint_id,
                    sequence=checkpoint.sequence,
                    storage_revision=current_revision or "missing",
                )
            if expected_storage_revision != current_revision:
                raise CheckpointConflictError("checkpoint storage revision changed")
            current_sequence = (
                int(current.get("sequence", -1)) if isinstance(current, dict) else -1
            )
            if checkpoint.sequence <= current_sequence:
                raise CheckpointConflictError("checkpoint sequence is stale")
            storage_revision = secrets.token_hex(16)
            archive["checkpoint"] = {
                "checkpoint_id": checkpoint.checkpoint_id,
                "sequence": checkpoint.sequence,
                "storage_revision": storage_revision,
                "reason": reason.value,
                "encrypted_payload": encrypt_json(self._checkpoint_record(checkpoint)),
            }
            archive.pop("initializing_process", None)
            archive["replay_position"] = self._capture_replay_position()
            archive["updated_at"] = time.time()
            self._write_archive(archive)
            return CheckpointSaveReceipt(
                checkpoint_id=checkpoint.checkpoint_id,
                sequence=checkpoint.sequence,
                storage_revision=storage_revision,
            )

    def _mark_pending_interaction_sync(
        self,
        request_id: str,
        turn_id: str | None,
    ) -> None:
        normalized_id = str(request_id or "").strip()
        if not normalized_id or len(normalized_id.encode("utf-8")) > 1024:
            raise ValueError("pending interaction id is invalid")
        normalized_turn = str(turn_id or "").strip()
        if len(normalized_turn.encode("utf-8")) > 1024:
            raise ValueError("pending interaction Turn id is invalid")
        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            pending = self._load_pending_interactions(archive)
            current = pending.get(normalized_id)
            record = {
                "turn_id": normalized_turn,
                "recorded_at": time.time(),
            }
            if current is not None:
                if current.get("turn_id") != normalized_turn:
                    raise ExecutionRecoveryUnavailableError(
                        "interaction id was reused by another Turn"
                    )
                return
            if len(pending) >= _MAX_PENDING_INTERACTIONS:
                raise ExecutionRecoveryUnavailableError(
                    "pending interaction recovery budget is exhausted"
                )
            pending[normalized_id] = record
            archive["encrypted_pending_interactions"] = encrypt_json(pending)
            archive["updated_at"] = time.time()
            self._write_archive(archive)

    def _clear_pending_interactions_sync(self) -> None:
        with self._archive_lock():
            archive = self._read_archive()
            self._validate_scope(archive)
            pending = self._load_pending_interactions(archive)
            if not pending:
                return
            archive.pop("encrypted_pending_interactions", None)
            archive["updated_at"] = time.time()
            self._write_archive(archive)

    def _decode_checkpoint(
        self,
        archive: dict[str, Any],
        *,
        agent_id: str,
        card: HarnessCard,
    ) -> HarnessCheckpoint:
        stored = archive.get("checkpoint")
        if not isinstance(stored, dict):
            raise ExecutionRecoveryUnavailableError("checkpoint is missing")
        encrypted = stored.get("encrypted_payload")
        if not isinstance(encrypted, str) or not encrypted:
            raise ExecutionRecoveryUnavailableError("checkpoint payload is missing")
        try:
            payload = decrypt_json(encrypted)
            if not isinstance(payload, dict):
                raise TypeError("checkpoint payload is not an object")
            checkpoint = HarnessCheckpoint(
                provider=payload["provider"],
                schema_version=payload["schema_version"],
                agent_id=payload["agent_id"],
                host_session_id=payload["host_session_id"],
                checkpoint_id=payload["checkpoint_id"],
                sequence=payload["sequence"],
                data=payload.get("data") or {},
                provider_session_id=payload.get("provider_session_id"),
                revision=stored.get("storage_revision"),
            )
        except Exception as exc:
            raise ExecutionRecoveryUnavailableError("checkpoint is unreadable") from exc
        if checkpoint.provider != card.name:
            raise ExecutionRecoveryUnavailableError("checkpoint belongs to another provider")
        if checkpoint.agent_id != agent_id:
            raise ExecutionRecoveryUnavailableError("checkpoint belongs to another agent")
        if checkpoint.host_session_id != self._session_id:
            raise ExecutionRecoveryUnavailableError("checkpoint belongs to another Session")
        if (
            checkpoint.checkpoint_id != stored.get("checkpoint_id")
            or checkpoint.sequence != stored.get("sequence")
        ):
            raise ExecutionRecoveryUnavailableError("checkpoint index does not match its payload")
        return checkpoint

    def _validate_scope(self, archive: dict[str, Any]) -> None:
        if archive.get("schema_version") != _RECOVERY_SCHEMA_VERSION:
            raise ExecutionRecoveryUnavailableError("recovery archive version is unsupported")
        if archive.get("execution_profile_id") != self._profile_id:
            raise ExecutionRecoveryUnavailableError("execution profile changed")
        if archive.get("binding") != self._binding_record():
            raise ExecutionRecoveryUnavailableError("execution Binding changed")
        if archive.get("runtime_paths") != self._paths_record():
            raise ExecutionRecoveryUnavailableError("execution paths changed")
        if archive.get("parent_session_id") != self._parent_session_id:
            raise ExecutionRecoveryUnavailableError("execution parent Session changed")

    def _validate_replay_position(self, archive: dict[str, Any]) -> None:
        replay = archive.get("replay_position")
        if not isinstance(replay, dict):
            raise ExecutionRecoveryUnavailableError("history replay position is missing")
        expected_name = replay.get("history_file")
        expected_offset = replay.get("byte_offset")
        if not isinstance(expected_name, str) or not isinstance(expected_offset, int):
            raise ExecutionRecoveryUnavailableError("history replay position is invalid")
        path = self._history_path
        if path.name != expected_name:
            raise ExecutionRecoveryUnavailableError("history storage format changed")
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError as exc:
            raise ExecutionRecoveryUnavailableError("history replay position is unreadable") from exc
        if size < expected_offset:
            raise ExecutionRecoveryUnavailableError("history was truncated before its replay position")

    @staticmethod
    def _load_pending_interactions(archive: dict[str, Any]) -> dict[str, dict[str, Any]]:
        encrypted = archive.get("encrypted_pending_interactions")
        if encrypted is None:
            return {}
        try:
            value = decrypt_json(str(encrypted))
        except Exception as exc:
            raise ExecutionRecoveryUnavailableError(
                "pending interaction recovery state is unreadable"
            ) from exc
        if not isinstance(value, dict) or len(value) > _MAX_PENDING_INTERACTIONS:
            raise ExecutionRecoveryUnavailableError(
                "pending interaction recovery state is invalid"
            )
        pending: dict[str, dict[str, Any]] = {}
        for request_id, record in value.items():
            if not isinstance(request_id, str) or not request_id:
                raise ExecutionRecoveryUnavailableError(
                    "pending interaction recovery state is invalid"
                )
            if not isinstance(record, dict) or not isinstance(
                record.get("turn_id"), str
            ):
                raise ExecutionRecoveryUnavailableError(
                    "pending interaction recovery state is invalid"
                )
            pending[request_id] = record
        return pending

    def _capture_replay_position(self) -> dict[str, Any]:
        flush_history_writes()
        path = self._history_path
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        return {"history_file": path.name, "byte_offset": int(size)}

    def _binding_record(self) -> dict[str, str]:
        binding = self._binding
        return {
            "subject_id": binding.subject_id,
            "host_session_id": binding.host_session_id,
            "workspace": binding.workspace,
            "provider_id": binding.provider_id,
            "config_revision": binding.config_revision,
            "fingerprint": binding.fingerprint,
        }

    def _paths_record(self) -> dict[str, str]:
        paths = self._runtime_paths
        return {
            "runtime_workspace_root": str(paths.runtime_workspace_root.resolve()),
            "cwd": str(paths.cwd.resolve()),
            "project_root": str(paths.project_root.resolve()),
            "outputs_dir": str(paths.outputs_dir.resolve()) if paths.outputs_dir else "",
        }

    @staticmethod
    def _card_record(card: HarnessCard) -> dict[str, str]:
        return {
            "name": card.name,
            "implementation_version": card.implementation_version,
            "protocol_version": card.protocol_version,
        }

    @staticmethod
    def _checkpoint_record(checkpoint: HarnessCheckpoint) -> dict[str, Any]:
        return {
            "provider": checkpoint.provider,
            "schema_version": checkpoint.schema_version,
            "agent_id": checkpoint.agent_id,
            "host_session_id": checkpoint.host_session_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "sequence": checkpoint.sequence,
            "data": json_value_to_builtin(checkpoint.data),
            "provider_session_id": checkpoint.provider_session_id,
        }

    def _read_archive(self) -> dict[str, Any]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ExecutionRecoveryUnavailableError("recovery archive is unreadable") from exc
        if not isinstance(value, dict):
            raise ExecutionRecoveryUnavailableError("recovery archive is invalid")
        return value

    @contextlib.contextmanager
    def _archive_lock(self):
        """Serialize archive transactions across threads and server processes."""

        with _LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._path.with_name(f".{self._path.name}.lock")
            try:
                with portalocker.Lock(str(lock_path), mode="a", timeout=10):
                    lock_path.chmod(0o600)
                    yield
            except portalocker.exceptions.LockException as exc:
                raise ExecutionRecoveryUnavailableError(
                    "recovery archive is busy"
                ) from exc

    def _write_archive(self, archive: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        ) as temporary_dir:
            temporary_path = Path(temporary_dir) / self._path.name
            temporary_path.touch(mode=0o600, exist_ok=False)
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump(archive, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            if os.name != "nt":
                directory_fd = os.open(
                    self._path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)


__all__ = [
    "ExecutionRecoveryPlan",
    "ExecutionRecoveryUnavailableError",
    "SessionExecutionRecovery",
]
