# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in lightweight Work research through existing execution and product builders.

Three real Provider cases request one report from the same two synthetic sources.
The automatic gate checks observable execution, reads and artifact delivery only;
material facts, reference support and the public final require independent review.
No model reasoning or credentials are archived by this observer.

Native uses the real adapter's research/subagent builders and core streaming path,
not the full Web/Surface admission stack. External uses EngineAgentAdapter with an
isolated admitted route. These component scenarios do not claim full channel E2E.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import shlex
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from .test_external_codex_product_route_local import _route

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get("RUN_WORK_RESEARCH_REMOTE") != "1", reason="real remote research is opt-in",
)]

# A single declared scenario budget, including delegation and final handoff.
# No completion-triggered extension and no retries to sample for a green result.
_RUN_BUDGET_S = 360
_CLEANUP_BUDGET_S = 30
_RESEARCH_TASK = (
    "Compare the two recorded retrieval runs in source-a.md and source-b.md: what do they "
    "establish about offline retrieval and benchmark comparability? These are two temporary "
    "four-line synthetic field logs, not user documents. Use the research agent and have it "
    "read both original files with tools. Use only these sources, no web searches. "
    "Save a concise report to research-report.md (aim for 300 words or fewer), covering the "
    "question/scope, findings/comparison, limitations and sources. Preserve the recorded names, "
    "dates and numerical observations accurately. Put source filenames and precise line "
    "locators next to material source-based claims. Distinguish an untested network requirement "
    "from an absent network dependency, and an observed run from an architecture-wide conclusion. "
    "Explain the limits of the comparison. Task scope and your method are not claims about "
    "the source's findings. Return the saved report path; report partial or failed execution "
    "honestly if the task cannot be completed."
)


def _write_sources(root: Path) -> None:
    root.mkdir()
    (root / "source-a.md").write_text(
        "# Pilot A field log\nDate: 2026-09-01\n"
        "Primary observation: offline retrieval took 42 seconds for 12 documents.\n"
        "All 12 documents were indexed locally. No network requirement was tested.\n",
    )
    (root / "source-b.md").write_text(
        "# Pilot B field log\nDate: 2026-09-02\n"
        "Primary observation: connected retrieval took 31 seconds for 12 documents.\n"
        "A network connection was required. The conditions differ from Pilot A; "
        "this is not a controlled benchmark and does not establish superiority.\n",
    )



def _read_content_hash(content):
    # ReadFileTool and CLI cat -n discard a final newline. Preserve all other
    # whitespace, blank lines, Markdown and source locator differences.
    return hashlib.sha256("\n".join(content.splitlines()).encode()).hexdigest()


def _parent_read_blocks(result):
    """Decode only public read-result envelopes, never arbitrary object reprs.

    The returned paths are completed-read metadata (when available). Numbered
    lines are unwrapped only as a complete sequential run, not with a blanket
    regex that could erase real text differences. A shell command may return
    several cat -n runs; each remains a separate candidate content block.
    """
    if getattr(result, "success", True) is False:
        return []
    if hasattr(result, "data"):
        result = result.data
    if isinstance(result, Mapping):
        if result.get("success") is False or result.get("isError") is True:
            return []
        path = result.get("file_path", result.get("path"))
        for key in ("data", "content", "text", "output", "result"):
            if key in result:
                return [(path or nested_path, text) for nested_path, text
                        in _parent_read_blocks(result[key])]
        return []
    if isinstance(result, (list, tuple)):
        return [block for part in result for block in _parent_read_blocks(part)]
    if not isinstance(result, str):
        return []
    path = None
    numbered = "cat"
    match = re.fullmatch(
        r"<path>([^\n]+)</path>\n(?:<type>file</type>\n)?<content>(.*)</content>\s*",
        result, re.S,
    )
    if match:
        path, result = match.groups()
        result = result.removeprefix("\n")
        result = re.sub(r"\n\n\(End of file - total \d+ lines\)\n?$", "", result)
        numbered = "opencode"
    elif result.startswith(("Chunk ID:", "Wall time:")) and "\nOutput:\n" in result:
        result = result.split("\nOutput:\n", 1)[1]
    lines = result.splitlines()
    pattern = r"(\d+): (.*)" if numbered == "opencode" else r"[ ]*(\d+)\t(.*)"
    parsed = [re.fullmatch(pattern, line) for line in lines]
    if parsed and all(parsed):
        if [int(item[1]) for item in parsed] == list(range(1, len(parsed) + 1)):
            return [(path, "\n".join(item[2] for item in parsed))]
        return []  # A partial/duplicated/reordered numbered read is not complete.
    if any(parsed):
        # Support actual shell multi-file cat -n readbacks without including
        # command headers or the next file in this report's digest.
        blocks, run = [], []
        for item in [*parsed, None]:
            if item and int(item[1]) == len(run) + 1:
                run.append(item[2])
                continue
            if run:
                blocks.append((path, "\n".join(run)))
                run = []
            if item and int(item[1]) == 1:
                run = [item[2]]
        return blocks
    return [(path, "\n".join(lines))]


def _parent_read_argument_paths(arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return set()
    if not isinstance(arguments, Mapping):
        return set()
    paths = {arguments[key] for key in ("file_path", "filePath", "path")
             if isinstance(arguments.get(key), str)}
    command = arguments.get("cmd", arguments.get("command"))
    if isinstance(command, str):
        try:
            paths.update(shlex.split(command))
        except ValueError:
            pass
    # Codex emits the executed shell command, occasionally with an outer -lc.
    for token in tuple(paths):
        if " " in token and any(name in token for name in ("source-a.md", "source-b.md", "research-report.md")):
            try:
                paths.update(shlex.split(token))
            except ValueError:
                pass
    cwd = arguments.get("cwd", arguments.get("workdir"))
    return {str(Path(cwd) / value) if cwd and not Path(value).is_absolute() else str(Path(value))
            for value in paths}


class _ResearchTrace:
    """Public observations only; a successful trace is not a semantic verdict."""

    def __init__(self, provider: str, root: Path):
        self.provider, self.root = provider, root
        self.original_sources = {name: (root / name).read_bytes() for name in ("source-a.md", "source-b.md")}
        self.started = time.monotonic()
        self.events = []
        self.outcome = "running"
        self.parent_final_text = ""
        self.parent_terminal = None
        self.child_providers = {}
        self.child_statuses = {}
        self.spawned_child_ids = set()
        self.source_reads = []
        self.report_reads = []
        self.model_request_contracts = []
        self._calls = {}

    def mark(self, stage, **details):
        self.events.append({"stage": stage, "seconds": round(time.monotonic() - self.started, 3), **details})

    def observe_read(self, scope, tool, arguments, result):
        name = tool.rsplit(".", 1)[-1].lower()
        if name not in {"read_file", "read", "exec_command", "bash", "shell"}:
            return
        paths = _parent_read_argument_paths(arguments)
        for observed_path, content in _parent_read_blocks(result):
            def is_path(filename):
                allowed = {self.root / filename, Path(filename)}
                return (Path(observed_path) in allowed if observed_path is not None
                        else bool(paths & {filename, str(self.root / filename)}))
            digest = _read_content_hash(content)
            for name in ("source-a.md", "source-b.md"):
                original = "\n".join(self.original_sources[name].decode().splitlines())
                # Shell reads may batch two complete files. Keep exact line
                # contents/boundaries while allowing surrounding tool output.
                complete = ("\n" + original + "\n") in ("\n" + content + "\n")
                if is_path(name) and complete:
                    source_hash = _read_content_hash(original)
                    self.source_reads.append({"scope": scope, "source": name, "sha256": source_hash})
                    self.mark("source_read", scope=scope, source=name, content_sha256=source_hash)
            if is_path("research-report.md"):
                self.report_reads.append({"scope": scope, "sha256": digest})
                self.mark("report_read", scope=scope, content_sha256=digest)

    def observe_control(self, tool, result):
        name = tool.rsplit(".", 1)[-1]
        data = getattr(result, "data", result)
        if name.endswith("subagent_spawn") and isinstance(data, Mapping) and isinstance(data.get("subagent_id"), str):
            self.spawned_child_ids.add(data["subagent_id"])
        if isinstance(data, Mapping) and isinstance(data.get("statuses"), Mapping):
            self.child_statuses.update(data["statuses"])
            self.mark("child_status", statuses=dict(data["statuses"]))
            return
        # Only the host's header is authoritative, not child-authored result text.
        text = "\n".join(content for _, content in _parent_read_blocks(data))
        header = text.split("\nresult:", 1)[0]
        ids = set(re.findall(r"(?:^|\n)subagent_id: ([\w-]+)(?:\n|$)", header))
        if name.endswith("subagent_spawn"):
            if isinstance(data, Mapping) and isinstance(data.get("subagent_id"), str):
                ids.add(data["subagent_id"])
            self.spawned_child_ids.update(ids)
        if name.endswith("subagent_wait"):
            statuses = dict(re.findall(r"(?:^|\n)subagent_id: ([\w-]+)\nstatus: ([\w-]+)(?:\n|$)", header))
            self.child_statuses.update(statuses)
            self.mark("child_status", statuses=statuses)

    def observe_external(self, envelope, provider):
        from openjiuwen.harness_protocol import ItemEventKind, ItemLifecycleEvent, TurnEventKind, TurnLifecycleEvent
        scope = envelope.host_session_id
        self.child_providers[scope] = provider
        self.spawned_child_ids.add(scope)
        event = envelope.event
        if isinstance(event, ItemLifecycleEvent) and event.item_type == "tool":
            data = event.data
            key = (scope, envelope.item_id)
            if event.kind in {ItemEventKind.STARTED, ItemEventKind.UPDATED}:
                self._calls[key] = data
            if event.kind is ItemEventKind.COMPLETED:
                previous = self._calls.pop(key, {})
                tool = data.get("name", data.get("tool_name", previous.get("name", "")))
                arguments = data.get("arguments", previous.get("arguments", {}))
                failed = bool(data.get("error")) or data.get("status") in {"failed", "declined"}
                failed = failed or data.get("opencode", {}).get("status") == "error"
                self.mark("child_tool_end", scope=scope, tool=tool, failed=failed)
                if not failed:
                    self.observe_read(scope, tool, arguments, data.get("result"))
        elif isinstance(event, TurnLifecycleEvent) and event.kind in {
            TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED,
        }:
            status = "completed" if event.kind is TurnEventKind.FINISHED else event.kind.value
            self.child_statuses[scope] = status
            self.mark("child_terminal", scope=scope, status=status)

    async def watch(self):
        observed = False
        while True:
            if not observed and (self.root / "research-report.md").is_file():
                observed = True
                self.mark("artifact_observed", artifact="research-report.md")
            await asyncio.sleep(0.25)

    def save(self):
        destination = os.environ.get("WORK_RESEARCH_EVIDENCE_DIR")
        if not destination:
            return
        output = Path(destination) / self.provider
        output.mkdir(parents=True, exist_ok=True)
        artifacts = {}
        for name in ("source-a.md", "source-b.md", "research-report.md"):
            path = self.root / name
            if path.is_file():
                body = path.read_bytes()
                (output / name).write_bytes(body)
                artifacts[name] = hashlib.sha256(body).hexdigest()
        (output / "timing.json").write_text(json.dumps({
            "schema_version": "lightweight-v1", "provider": self.provider, "outcome": self.outcome,
            "semantic_review": "pending_independent_review", "execution_budget_seconds": _RUN_BUDGET_S,
            "cleanup_budget_seconds": _CLEANUP_BUDGET_S, "parent_terminal": self.parent_terminal,
            "child_providers": self.child_providers, "child_statuses": self.child_statuses,
            "spawned_child_ids": sorted(self.spawned_child_ids), "source_reads": self.source_reads,
            "report_reads": self.report_reads, "artifact_sha256": artifacts,
            "original_source_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in self.original_sources.items()},
            "events": self.events,
        }, indent=2))
        (output / "parent-final.txt").write_text(self.parent_final_text)
        (output / "model-request-contracts.json").write_text(json.dumps(self.model_request_contracts, indent=2))



def _observe_external_children(factory, trace):
    """Attach to the existing observer fanout; no second event consumer.

    Startup can publish before the factory registers _live. Only create's actual
    returned binding establishes Provider identity; an early startup notice must
    never fail the running session or be mistaken for a completed child.
    """
    original_create = factory.create
    original_factory = factory._event_observer_factory
    async def create(*args, **kwargs):
        execution = await original_create(*args, **kwargs)
        binding = execution.binding
        trace.child_providers[binding.host_session_id] = binding.provider_id
        trace.spawned_child_ids.add(binding.host_session_id)
        trace.mark("child_bound", scope=binding.host_session_id, provider=binding.provider_id)
        return execution
    def observers(child_id, subject_id):
        original = original_factory(child_id, subject_id) if original_factory else None
        async def observe(envelope):
            if original:
                await original(envelope)
            provider = trace.child_providers.get(child_id)
            if provider is None:
                trace.mark("child_startup_event", scope=child_id)
                return
            trace.observe_external(envelope, provider)
        return observe
    factory.create = create
    factory._event_observer_factory = observers


def _source_citation_ranges(text, source):
    return [(int(m[1]), int(m[2] or m[1])) for m in re.finditer(
        re.escape(source) + r"`?\s*(?:#L|[:,]\s*L?\s*|L\s*|lines?\s+)(\d+)"
        r"(?:\s*[-–]\s*L?(\d+))?\b", text, re.I,
    )]


def _check_report(root):
    """Mechanical artifact/locator availability, never a truth or entailment check."""
    path = root / "research-report.md"
    assert path.is_file(), "Requested research report was not saved"
    report = path.read_text()
    assert report.strip(), "Saved research report is empty"
    for name in ("source-a.md", "source-b.md"):
        ranges = _source_citation_ranges(report, name)
        assert ranges, f"No inspectable line locator for {name}"
        assert all(1 <= first <= last <= 4 for first, last in ranges), f"Out-of-bounds locator for {name}"
    return report


def _check_execution_delivery(trace):
    assert trace.parent_terminal == "completed", "Parent has no successful terminal"
    assert trace.spawned_child_ids, "No real research child observed"
    assert set(trace.child_providers) == trace.spawned_child_ids, "Missing actual child Provider identity"
    assert set(trace.child_providers.values()) == {trace.provider}, "Child changed Provider"
    assert all(trace.child_statuses.get(sid) == "completed" for sid in trace.spawned_child_ids), (
        "A research child has no successful terminal"
    )
    inspected = {r["source"] for r in trace.source_reads if r["scope"] != "parent"}
    assert inspected == {"source-a.md", "source-b.md"}, "Research child did not read both complete originals"
    assert all((trace.root / name).read_bytes() == original for name, original in trace.original_sources.items()), (
        "An original source was changed during execution"
    )
    report = _check_report(trace.root)
    assert "research-report.md" in trace.parent_final_text, "Parent did not return the requested artifact path"
    trace.mark("delivery_observed", report_words=len(report.split()))
    # Semantic approval, including whether the public final honestly reflects
    # the report, is deliberately outside this machine-observable contract.
    trace.outcome = "passed"


def _native_research_work_config(root):
    from openjiuwen.core.sys_operation import LocalWorkConfig
    from jiuwenswarm.agents.harness.work.research import _RESEARCH_SKILLS
    return LocalWorkConfig(restrict_to_sandbox=True, sandbox_root=[str(root), str(_RESEARCH_SKILLS)],
                           shell_allowlist=[], dangerous_patterns=[r"[\s\S]"])


def _native_research_parent(model, root, operation, child_trace, parent_trace):
    """Product research/subagent recipes + original core stream, not Web ingress."""
    from unittest.mock import patch
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir, adapter._sys_operation = str(root), operation
    config = {"subagents": {"research_agent": {"enabled": True, "max_iterations": 12},
                             "statusline_setup_agent": {"enabled": False}}}
    # This file-only fixture does not initialize Browser or load connected MCP
    # credentials. Actual research factory and tool routing are not replaced.
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False), \
         patch.object(adapter, "_sync_mcp_credentials_environment", return_value=False), \
         patch.object(adapter, "_resolve_runtime_language", return_value="en"):
        specs, _ = adapter._build_configured_subagents(model, config, {})
        rail = adapter._build_subagent_rail({"react": {"subagent_runtime": {"enabled": True}}})
    spec = next(s for s in specs if s.agent_card.name == "research_agent")
    assert spec.model is model and spec.sys_operation is operation
    spec.enable_read_image_multimodal = False
    spec.rails.append(child_trace)
    parent = create_deep_agent(
        model=model, workspace=str(root), sys_operation=operation, subagents=[spec],
        rails=[SysOperationRail(), rail, parent_trace], enable_subagent_runtime=True,
        max_iterations=12, enable_task_loop=False, enable_read_image_multimodal=False,
        language="en", system_prompt="Complete the user's assignment within the admitted workspace and tool permissions.",
    )
    return parent


def _observe_native_model_requests(monkeypatch, trace):
    from openjiuwen.core.foundation.llm import Model
    from jiuwenswarm.agents.harness.work.research import work_research_instructions
    invoke, stream = Model.invoke, Model.stream
    def observe(messages, tools):
        def field(value, key, default=None):
            return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)
        system = "\n".join(str(field(m, "content", "")) for m in messages if field(m, "role") == "system")
        names = {field(t, "name") or field(field(t, "function", {}), "name") for t in tools or []}
        trace.model_request_contracts.append({
            "system_sha256": hashlib.sha256(system.encode()).hexdigest(), "system_characters": len(system),
            "research_instructions_present": work_research_instructions() in system,
            "tool_names": sorted(n for n in names if isinstance(n, str)),
        })
    @functools.wraps(invoke)
    async def observed_invoke(self, messages, **kwargs):
        observe(messages, kwargs.get("tools"))
        return await invoke(self, messages, **kwargs)
    @functools.wraps(stream)
    async def observed_stream(self, messages, **kwargs):
        observe(messages, kwargs.get("tools"))
        async for chunk in stream(self, messages, **kwargs):
            yield chunk
    monkeypatch.setattr(Model, "invoke", observed_invoke)
    monkeypatch.setattr(Model, "stream", observed_stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_work_research_real_external_cited_artifact(tmp_path: Path, provider: str, monkeypatch):
    if provider == "codex":
        pytest.importorskip("openai_codex")
    root = tmp_path / "workspace"
    _write_sources(root)
    trace = _ResearchTrace(provider, root)
    model = {
        "model": os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
        "provider": "work_research_remote",
        "api_base": os.environ["WORK_RESEARCH_API_BASE"],
        "api_key": os.environ["WORK_RESEARCH_API_KEY"],
    }
    if provider == "codex":
        home, codex_home = tmp_path / "home", tmp_path / "codex"
        for path in (home, codex_home, codex_home / "skills"):
            path.mkdir()
        provider_config = {
            "inherit_process_env": False,
            "env": {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
            "startup_source_roots": [str(root), str(codex_home / "skills")],
            "mcp_required": True,
            "model": model,
            # Do not silently retry a stalled turn and multiply the test budget.
            "turn_idle_timeout_s": 100,
            "turn_idle_retries": 0,
        }
    else:
        model["max_output_tokens"] = 8192
        trace.mark("model_configured", max_output_tokens=model["max_output_tokens"])
        cli = Path(
            os.environ.get(
                "WORK_RESEARCH_OPENCODE_CLI",
                str(Path.home() / ".opencode/bin/opencode"),
            )
        )
        assert cli.is_file(), (
            "A real OpenCode CLI is required for the selected provider"
        )
        runtime_root = tmp_path / "opencode-runtime"
        runtime_root.mkdir(mode=0o700)
        provider_config = {
            "cli_path": str(cli),
            "runtime_root": str(runtime_root),
            "model": model,
            "turn_timeout_s": _RUN_BUDGET_S,
        }
    spec = AgentExecutionSpec(
        provider,
        "r1-12-closure",
        authorization=ExecutionAuthorization(full_access=True),
        provider_config=provider_config,
    )
    route = _route(root, spec)
    adapter = EngineAgentAdapter(route)
    query = _RESEARCH_TASK
    request = AgentRequest(request_id="r1-12-research", channel_id="web", session_id="r1-a2-session",
                           params={"mode": "agent", "query": query}, is_stream=True)
    request._execution_route = route
    runtime, watcher = None, None
    parent_text = []
    try:
        trace.mark("construction_start")
        await adapter.create_instance(mode="agent")
        runtime = adapter._subagent_runtime
        _observe_external_children(runtime._factory, trace)
        watcher = asyncio.create_task(trace.watch())
        adapter.select_execution_for_request(request)
        trace.mark("execution_start")
        async with asyncio.timeout(_RUN_BUDGET_S):
            async for chunk in adapter.process_message_stream_impl(request, {"query": query}):
                payload = chunk.payload or {}
                event = payload.get("event_type")
                if event == "chat.tool_result":
                    trace.mark("parent_tool_end", tool=payload.get("tool_name", ""))
                elif event == "chat.delta":
                    parent_text.append(payload.get("content", ""))
                elif event == "chat.final":
                    trace.parent_terminal = payload.get("terminal_status")
                    trace.mark("parent_terminal", terminal=trace.parent_terminal)
        trace.parent_final_text = "".join(parent_text)
        _check_execution_delivery(trace)
    except BaseException as exc:
        trace.outcome = type(exc).__name__
        trace.mark("failed", exception_type=type(exc).__name__)
        raise
    finally:
        trace.parent_final_text = "".join(parent_text)
        trace.mark("cleanup_start")
        try:
            async with asyncio.timeout(_CLEANUP_BUDGET_S):
                await adapter.cleanup()
            assert runtime is None or not runtime.has_control()
            trace.mark("cleanup_complete")
        except BaseException as exc:
            trace.outcome = "cleanup_" + type(exc).__name__
            trace.mark("cleanup_failed", exception_type=type(exc).__name__)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            trace.save()


@pytest.mark.asyncio
async def test_work_research_real_native_cited_artifact(tmp_path: Path, monkeypatch):
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.sys_operation import SysOperationCard, OperationMode
    from openjiuwen.core.sys_operation.cwd import init_cwd
    root = tmp_path / "workspace"
    _write_sources(root)
    trace = _ResearchTrace("native", root)
    _observe_native_model_requests(monkeypatch, trace)
    class Trace(AgentRail):
        def __init__(self, scope):
            self.scope = scope
        def fork_for_agent(self):
            return type(self)(self.scope)
        async def before_model_call(self, ctx):
            if self.scope == "child":
                child_id = ctx.session.get_session_id()
                trace.spawned_child_ids.add(child_id)
                trace.child_providers[child_id] = "native"
                trace.mark("child_model_start", scope=child_id)
            else:
                trace.mark("parent_model_start")
        async def after_model_call(self, ctx):
            trace.mark("model_end", scope=self.scope,
                       finish_reason=getattr(ctx.inputs.response, "finish_reason", None))
        async def after_tool_call(self, ctx):
            trace.mark("tool_end", scope=self.scope, tool=ctx.inputs.tool_name)
            scope = ctx.session.get_session_id() if self.scope == "child" else "parent"
            trace.observe_read(scope, ctx.inputs.tool_name, ctx.inputs.tool_args, ctx.inputs.tool_result)
            if self.scope == "parent":
                trace.observe_control(ctx.inputs.tool_name, ctx.inputs.tool_result)
    model = Model(model_client_config=ModelClientConfig(
        client_provider="OpenAI", api_base=os.environ["WORK_RESEARCH_API_BASE"],
        api_key=os.environ["WORK_RESEARCH_API_KEY"], timeout=90,
    ), model_config=ModelRequestConfig(model=os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
                                      temperature=0.1, max_tokens=8192))
    card = SysOperationCard(id="r1-12-native", mode=OperationMode.LOCAL, work_config=_native_research_work_config(root))
    parent, watcher = None, asyncio.create_task(trace.watch())
    await Runner.start()
    try:
        trace.mark("construction_start")
        Runner.resource_mgr.add_sys_operation(card)
        operation = Runner.resource_mgr.get_sys_operation(card.id)
        init_cwd(str(root), workspace=str(root), project_root=str(root))
        parent = _native_research_parent(model, root, operation, Trace("child"), Trace("parent"))
        trace.mark("execution_start", model_max_tokens=8192)
        result = None
        async with asyncio.timeout(_RUN_BUDGET_S):
            async for chunk in Runner.run_agent_streaming(parent, {"query": _RESEARCH_TASK}, session="r1-12-native-session"):
                kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
                payload = chunk.get("payload") if isinstance(chunk, dict) else getattr(chunk, "payload", None)
                if kind == "llm_output" and isinstance(payload, dict):
                    trace.parent_final_text += str(payload.get("content", ""))
                if kind == "answer" and isinstance(payload, dict):
                    assert result is None, "Unexpected duplicate Native terminal"
                    result = payload
                    trace.parent_final_text = str(payload.get("output", ""))
                    trace.parent_terminal = "completed" if payload.get("result_type") == "answer" else "failed"
                    trace.mark("parent_terminal", terminal=trace.parent_terminal)
        _check_execution_delivery(trace)
    except BaseException as exc:
        trace.outcome = type(exc).__name__
        trace.mark("failed", exception_type=type(exc).__name__)
        raise
    finally:
        trace.mark("cleanup_start")
        try:
            async with asyncio.timeout(_CLEANUP_BUDGET_S):
                if parent is not None:
                    from openjiuwen.harness.tools.subagent import release_subagent_control
                    await release_subagent_control(parent, "r1-12-native-session", reason="test_finished")
                    await parent.stop()
                Runner.resource_mgr.remove_sys_operation(sys_operation_id=card.id)
                await Runner.stop()
            trace.mark("cleanup_complete")
        except BaseException as exc:
            trace.outcome = "cleanup_" + type(exc).__name__
            trace.mark("cleanup_failed", exception_type=type(exc).__name__)
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            trace.save()
