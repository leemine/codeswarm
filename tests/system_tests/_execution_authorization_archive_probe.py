# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit old-writer/new-reader probe; never uses the user's session storage.

Export old engine/config.py and runtime/harness/config_source.py with git show.
Run write with those paths, then read in another process with the same --root.
The recovery codec is unchanged; the writer loads the old binding/catalog code.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import (
    CheckpointReason,
    HarnessCapability,
    HarnessCard,
    HarnessCheckpoint,
    ResumePolicy,
)

from jiuwenswarm.common.auth import session_store
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness import recovery_store
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog


def _original(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("write", "read"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--old-engine", type=Path)
    parser.add_argument("--old-catalog", type=Path)
    args = parser.parse_args()
    binding_type, catalog_loader = ExecutionBinding, load_execution_catalog
    root = args.root.resolve()
    if args.mode == "write":
        if args.old_engine is None or args.old_catalog is None:
            parser.error("write requires both original source paths")
        root.mkdir(parents=True, exist_ok=False)
        binding_type = _original("old_engine_config", args.old_engine).ExecutionBinding
        catalog_loader = _original("old_config_source", args.old_catalog).load_execution_catalog
        (root / "auth").mkdir()
    elif not (root / "auth").is_dir():
        parser.error("read requires an existing probe archive root")

    def resolve(session_id, create=False):
        path = root / "sessions" / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path, None

    recovery_store.resolve_session_dir = resolve
    recovery_store.get_read_history_path = lambda sid: root / "sessions" / sid / "history.jsonl"
    session_store.auth_dir = lambda: root / "auth"
    for full_access in (False, True):
        sid = "old-full" if full_access else "old-normal"
        cwd = root / sid
        cwd.mkdir(exist_ok=True)
        config = {
            "permissions": {"enabled": not full_access},
            "execution": {"default_profile_id": "codex", "profiles": {
                "codex": {"provider_id": "codex", "config_revision": "old-r1",
                          "provider_config": {"model": {"model": "fixture"}}},
            }},
        }
        spec = catalog_loader(config).source().resolve()
        binding = binding_type.create(spec, subject_id="alice", host_session_id=sid, workspace=str(cwd))
        paths = RuntimeWorkspacePaths(internal_workspace_dir=cwd, runtime_workspace_root=cwd, cwd=cwd,
                                      project_root=cwd)
        recovery = recovery_store.SessionExecutionRecovery(
            session_id=sid, execution_profile_id="codex", binding=binding, runtime_paths=paths,
        )
        card = HarnessCard(name="codex", implementation_version="1.0",
                           capabilities=frozenset({HarnessCapability.CHECKPOINT}))
        agent_id = "external:codex:" + sid
        if args.mode == "write":
            assert recovery.prepare(card, agent_id=agent_id).resume_policy is ResumePolicy.NEW
            await recovery.save(HarnessCheckpoint(
                provider="codex", schema_version="1", agent_id=agent_id, host_session_id=sid,
                checkpoint_id="checkpoint-" + sid, sequence=1, data={"thread_id": "thread-" + sid},
                provider_session_id="thread-" + sid,
            ), reason=CheckpointReason.TURN_COMPLETED)
            (root / (sid + ".fingerprint")).write_text(binding.fingerprint, encoding="utf-8")
            print("OLD-WRITE", sid, binding.fingerprint)
        else:
            assert binding.fingerprint == (root / (sid + ".fingerprint")).read_text(encoding="utf-8")
            restored = recovery.prepare(card, agent_id=agent_id)
            assert restored.resume_policy is ResumePolicy.REQUIRE_RESUME
            assert restored.checkpoint.data["thread_id"] == "thread-" + sid
            print("NEW-READ", sid, binding.fingerprint, "REQUIRE_RESUME")


if __name__ == "__main__":
    asyncio.run(main())
