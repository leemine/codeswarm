# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Protected-project qualification with real product construction and execution.

RUN_GOVERNED_PRODUCT_CLOSURE=1 runs each case in a task-owned systemd cgroup.
AgentServer, Runtime, AgentManager, Native/CLI providers, disk ACL and history
are real. Only the remote model is replaced by a loopback protocol fixture.
No Gateway/browser or remote authentication acceptance is claimed here.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.system, pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("RUN_GOVERNED_PRODUCT_CLOSURE") != "1",
                                 reason="real governed product construction is opt-in")]


def _configure(data: Path, provider: str, base_url: str, cli: str) -> None:
    import yaml
    from tests.system_tests.test_native_process_cli_channel_local import _configure_workspace
    _configure_workspace(data, base_url)
    path = data / "config" / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["permissions"]["enabled"] = False
    if provider != "native":
        home = data / "provider-home"
        codex_home = data / "codex-home"
        runtime_root = data / "opencode-runtime"
        for directory in (home, codex_home, codex_home / "skills", runtime_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        provider_config = {
            "model": {"model": "gpt-5.6-sol" if provider == "codex" else "fixture",
                      "provider": "governed_fixture", "api_base": base_url, "api_key": "fixture-only"},
        }
        if provider == "codex":
            provider_config.update({
                "inherit_process_env": False,
                "env": {"HOME": str(home), "CODEX_HOME": str(codex_home),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                "mcp_required": True,
            })
        else:
            provider_config.update({"cli_path": cli, "runtime_root": str(runtime_root), "turn_timeout_s": 45})
        config["execution"] = {"default_profile_id": "governed", "profiles": {
            "governed": {"provider_id": provider, "config_revision": "closure-v1",
                         "authorization": {"full_access": True}, "provider_config": provider_config},
        }}
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


@pytest.mark.parametrize("provider", ["native", "codex", "opencode"])
@pytest.mark.timeout(200)
def test_protected_project_real_product_construction(tmp_path, provider):
    from tests.system_tests.test_external_codex_product_route_local import _ResponsesFixture
    from tests.system_tests.test_native_process_cli_channel_local import _ChatCompletionsFixture
    cli = os.environ.get("OPENCODE_OC1_CLI", str(Path.home() / ".opencode/bin/opencode"))
    if provider == "codex":
        pytest.importorskip("openai_codex", reason="real bundled Codex CLI required")
    if provider == "opencode":
        assert Path(cli).is_file(), f"OpenCode CLI unavailable: {cli}"
    data = tmp_path / "data"
    unit = f"r1-governed-{provider}-{uuid.uuid4().hex[:10]}"
    fixture_type = _ResponsesFixture if provider == "codex" else _ChatCompletionsFixture
    with fixture_type() as fixture:
        _configure(data, provider, fixture.base_url, cli)
        env = {
            "HOME": str(tmp_path / "home"), "JIUWENSWARM_DATA_DIR": str(data),
            "JIUWENSWARM_HOME": str(tmp_path / "home"), "JIUWENSWARM_CONFIG_URL": "off",
            "JIUWENSWARM_RUNTIME_WORKSPACE_READY": "1",
            "PYTHONPATH": str(REPO_ROOT), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }
        output = tmp_path / "closure.json"
        command = ["systemd-run", "--user", "--quiet", "--wait", "--collect", "--pipe",
                   f"--unit={unit}", "--property=RuntimeMaxSec=160", "--property=MemoryMax=3G",
                   "--property=TasksMax=384", "--property=KillMode=control-group",
                   "/usr/bin/env", *(f"{name}={value}" for name, value in env.items()),
                   sys.executable, str(Path(__file__).resolve()), "--worker", provider, str(data), str(output)]
        try:
            result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            result = subprocess.CompletedProcess(command, 124, decoded(exc.stdout),
                                                 decoded(exc.stderr) + "\nWorker exceeded 180s timeout")
        finally:
            # Only this test's random unit is addressed. KillMode closes all of
            # its descendants even if a Provider or worker failed to finalize.
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=15)
        (tmp_path / "worker.log").write_text(result.stdout + result.stderr)
        evidence = {"provider": provider, "unit": unit, "returncode": result.returncode,
                    "model_requests": len(fixture.requests),
                    "foreground_model_requests": sum(bool(body.get("stream")) for body in fixture.requests),
                    "swarm_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip(),
                    "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "core_source": json.loads(importlib.metadata.distribution("openjiuwen").read_text("direct_url.json")),
                    "python": sys.executable,
                    "worker_log": str(tmp_path / "worker.log")}
        if provider == "codex":
            evidence["provider_version"] = importlib.metadata.version("openai-codex")
        elif provider == "opencode":
            evidence["provider_version"] = subprocess.check_output([cli, "--version"], env={**os.environ, **env}, text=True, timeout=15).strip()
        if output.exists():
            evidence.update(json.loads(output.read_text()))
        destination = os.getenv("R1_THREE_EVIDENCE_DIR")
        if destination:
            Path(destination).mkdir(parents=True, exist_ok=True)
            (Path(destination) / f"governed-product-{provider}.json").write_text(json.dumps(evidence, indent=2))
            (Path(destination) / f"governed-product-{provider}.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-15000:]
        assert evidence["runtime_closed"] and evidence["agent_manager_empty"]
        rendered = json.dumps(fixture.requests)
        assert "DENIED-READ-ONLY" not in rendered and "DENIED-REVOKED" not in rendered
        # Native also makes non-streaming capability/context requests. Preserve
        # those product behaviors and account for them separately from turns.
        assert evidence["foreground_model_requests"] == 2, evidence
        if provider != "native":
            assert len(fixture.requests) == 2, evidence
        assert evidence["history_final_count"] == 2, evidence


async def _worker(provider: str, data: Path, output: Path) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.governance.contracts import TrustedIdentity
    from jiuwenswarm.governance.preparation import GovernanceError
    from jiuwenswarm.runtime.session.model import SessionRequestDuplicateError
    from jiuwenswarm.runtime.session_provisioner import SessionCreateInput, SessionProvisionState
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session import project_store
    from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

    trace = []
    result = {"provider": provider, "trace": trace}
    root = data / "project"
    root.mkdir()
    work_mode = "work" if provider == "native" else "code"
    mode = f"agent.{work_mode}.normal"
    project = project_store.create_project("Governed closure", str(root), work_mode)
    access = ProjectAccessStore()
    access.initialize(project.project_id, "owner")
    revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read"]}, expected_revision=1)
    identity = TrustedIdentity("member", "governed-executor", "closure-authenticated-host")
    server = AgentWebSocketServer(trusted_identity_resolver=lambda _: identity)
    runtime = server.get_runtime()
    manager = runtime.agent_manager
    result["manager_class"] = type(manager).__qualname__
    result["runtime_class"] = type(runtime).__qualname__
    result["cgroup"] = Path("/proc/self/cgroup").read_text().strip()
    profile = None if provider == "native" else "governed"

    def create(token):
        return SessionCreateInput(channel_id="web", create_token=token, mode=mode,
                                  project_id=project.project_id, project_dir=str(root),
                                  work_mode=work_mode, execution_profile_id=profile,
                                  persist_session=True, persist_session_supplied=True)

    async def deny(coro, label):
        try:
            await coro
        except GovernanceError:
            trace.append(label)
        else:
            raise AssertionError(f"expected governance denial: {label}")

    try:
        await runtime.start()
        trace.append("real-runtime-started")
        await deny(runtime.prepare_session_create(create("read-only")), "read-only-create-denied")
        revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read", "execute"]}, expected_revision=revision)
        prepared = await runtime.prepare_session_create(create("revoke-before-commit"))
        revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read"]}, expected_revision=revision)
        await deny(runtime.commit_session_provision(prepared, timing=prepared.commit_timing), "commit-revocation-denied")
        assert prepared.state is SessionProvisionState.ABORTED
        trace.append("owned-provision-aborted")
        revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read", "execute"]}, expected_revision=revision)
        prepared = await runtime.prepare_session_create(create("allowed"))
        created = await runtime.commit_session_provision(prepared, timing=prepared.commit_timing)
        sid = created.session_id
        trace.append("real-session-created")

        def request(request_id, query):
            return AgentRequest(request_id, channel_id="web", session_id=sid, req_method=ReqMethod.CHAT_SEND,
                                params={"mode": mode, "work_mode": work_mode, "query": query,
                                        "project_id": project.project_id, "project_dir": str(root)},
                                user_id="forged-wire-owner", is_stream=True)

        async def collect(req):
            events = [event async for event in runtime.stream(req, trigger_hook=False)]
            assert not any(not event.ok for event in events), [event.payload for event in events]
            assert any(event.event_type == "chat.final" for event in events), [event.payload for event in events]
            return events

        first = request("first", "Only reply R1-04A-NATIVE-FIRST")
        first_events = await collect(first)
        first_output = "R1-A2-PRODUCT-ROUTE-OK" if provider == "codex" else "R1-04A-NATIVE-FIRST"
        assert first_output in json.dumps([event.payload for event in first_events])
        trace.append("real-first-turn")
        if provider != "native":
            binding = manager._session_execution_bindings[("web", sid)]
            assert binding.subject_id == "governed-executor"
            result["execution_subject_id"] = binding.subject_id
            trace.append("trusted-execution-subject")
        try:
            await collect(request("first", "Only reply R1-04A-NATIVE-FIRST"))
        except (SessionRequestDuplicateError, GovernanceError):
            trace.append("duplicate-blocked")
        else:
            raise AssertionError("duplicate was not rejected")
        revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read"]}, expected_revision=revision)
        await deny(collect(request("read-only-turn", "DENIED-READ-ONLY")), "read-only-turn-denied")
        revision = access.replace_acl(project.project_id, "owner", acl={}, expected_revision=revision)
        await deny(collect(request("revoked-turn", "DENIED-REVOKED")), "revoked-turn-denied")
        revision = access.replace_acl(project.project_id, "owner", acl={"member": ["read", "execute"]}, expected_revision=revision)
        second_events = await collect(request("second", "Only reply R1-04A-NATIVE-SECOND"))
        second_output = "R1-A2-PRODUCT-ROUTE-OK" if provider == "codex" else "R1-04A-NATIVE-SECOND"
        assert second_output in json.dumps([event.payload for event in second_events])
        trace.append("real-continue-turn")
        history = data / "agent" / "sessions" / sid / "history.jsonl"
        records = [json.loads(line) for line in history.read_text().splitlines() if line.strip()]
        finals = [record for record in records if record.get("event_type") == "chat.final"]
        result.update(session_id=sid, history_final_count=len(finals), history_path=str(history),
                      agent_classes=[type(agent).__qualname__ for modes in manager.agents.values() for agent in modes.values()])
        trace.append("history-read")
        await deny(runtime.delete_session(channel_id="web", session_id=sid), "executor-delete-denied")
        assert history.exists()
        identity = TrustedIdentity("owner", "governed-executor", "closure-authenticated-host")
        deleted = await runtime.delete_session(channel_id="web", session_id=sid)
        assert deleted.ok, deleted
        assert not history.parent.exists()
        trace.append("owner-deleted-session")
    finally:
        try:
            await runtime.close()
            result["runtime_closed"] = runtime.closed
            result["agent_manager_empty"] = not manager.agents
        finally:
            output.write_text(json.dumps(result, indent=2))


if __name__ == "__main__" and "--worker" in sys.argv:
    asyncio.run(_worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])))
