# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real Chrome/MCP acceptance for the External Browser product bridge."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from aiohttp import web

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.subagent_runtime import SubagentBuildRequest
from openjiuwen.harness.subagent_runtime import (
    ParentExecutionContext,
    SubagentTurnRequest,
)
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    ExecutionAuthorization,
    ToolInvocation,
)

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.external_browser import (
    build_external_browser_resources,
)
from jiuwenswarm.runtime.harness.external_browser_admission import (
    ExternalBrowserAdmission,
)
from jiuwenswarm.runtime.harness.external_subagent import (
    ExternalSubagentExecutionFactory,
)
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_EXTERNAL_BROWSER_REAL") != "1",
        reason="real External Browser acceptance is opt-in",
    ),
]


class _LocalPage(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits += 1
        if self.path == "/slow-download.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="slow.bin"')
            self.send_header("Content-Length", str(32 * 1024 * 1024))
            self.end_headers()
            try:
                for _ in range(512):
                    self.wfile.write(b"x" * 65536)
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/login":
            body = (
                b"<!doctype html><title>R1-10D Login</title>"
                b'<form method="post" action="/login">'
                b'<label>Username <input name="username"></label>'
                b'<label>Password <input name="password" type="password"></label>'
                b'<button type="submit">Sign in</button></form>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/account":
            signed_in = "r1_10d_session=accepted" in self.headers.get("Cookie", "")
            body = (
                b"<!doctype html><title>R1-10D Account</title>"
                + (
                    b"<h1>R1-10D SIGNED IN</h1>"
                    if signed_in
                    else b"<h1>R1-10D SIGN IN REQUIRED</h1>"
                )
            )
            self.send_response(200 if signed_in else 401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/download.txt":
            body = b"R1-10D REAL DOWNLOAD"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="download-fixture.txt"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = (
            b"<!doctype html><title>R1-10D Browser</title>"
            b"<h1>R1-10D REAL BROWSER PASS</h1>"
            b'<button onclick="document.getElementById(\'upload\').click()">'
            b"Upload fixture</button>"
            b'<input id="upload" type="file" hidden '
            b'onchange="const file=this.files[0];const reader=new FileReader();'
            b"reader.onload=()=>document.getElementById('upload-result').textContent="
            b"file.name+':'+reader.result;reader.readAsText(file)\">"
            b'<output id="upload-result">No upload</output>'
            b'<a href="/download.txt" download>Download fixture</a>'
            b'<a href="/slow-download.bin" download>Slow download fixture</a>'
            b'<a href="/login">Login fixture</a>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits += 1
        if self.path != "/login":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            values.get("username") == ["r1-10d-user"]
            and values.get("password") == ["local-secret"]
        )
        if not valid:
            self.send_error(403)
            return
        self.send_response(303)
        self.send_header("Location", "/account")
        self.send_header(
            "Set-Cookie",
            "r1_10d_session=accepted; Max-Age=3600; Path=/; HttpOnly; SameSite=Strict",
        )
        self.end_headers()


class _ResponsesFixture:
    """Local Responses API that drives the real bundled Codex CLI."""

    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return None

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                body = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                owner.requests.append(body)
                item = owner.items.pop(0)
                response = {
                    "id": f"resp_{len(owner.requests)}",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                }
                events = [
                    ("response.created", {"response": response}),
                    (
                        "response.output_item.added",
                        {"output_index": 0, "item": item},
                    ),
                    (
                        "response.output_item.done",
                        {"output_index": 0, "item": item},
                    ),
                    (
                        "response.completed",
                        {
                            "response": {
                                **response,
                                "status": "completed",
                                "output": [item],
                                "usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "total_tokens": 2,
                                },
                            }
                        },
                    ),
                ]
                data = "".join(
                    "event: "
                    + name
                    + "\ndata: "
                    + json.dumps({"type": name, **payload})
                    + "\n\n"
                    for name, payload in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _ChatCompletionsFixture:
    """Local Chat Completions API that drives the real OpenCode CLI."""

    def __init__(self, actions: list[dict]) -> None:
        self.actions = list(actions)
        self.requests: list[dict] = []

    async def respond(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        action = self.actions.pop(0)
        if "tool" in action:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {
                            "name": action["tool"],
                            "arguments": json.dumps(action["args"]),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {"content": action["text"]}
            finish = "stop"
        base = {
            "id": f"chatcmpl-r1-10d-{len(self.requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture",
        }
        chunks = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", **delta},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 5,
                    "total_tokens": 22,
                },
            },
        ]
        data = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return web.Response(text=data, content_type="text/event-stream")


@pytest.fixture
def local_page_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalPage)
    _LocalPage.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _paths(tmp_path: Path) -> RuntimeWorkspacePaths:
    workspace = (tmp_path / "workspace").resolve()
    cwd = workspace / "task"
    cwd.mkdir(parents=True)
    return RuntimeWorkspacePaths(
        internal_workspace_dir=workspace / ".internal",
        runtime_workspace_root=workspace,
        cwd=cwd,
        project_root=workspace,
        outputs_dir=workspace / "outputs",
    )


def _route(paths: RuntimeWorkspacePaths, spec: AgentExecutionSpec) -> AdmittedExecutionRoute:
    source = ExecutionConfigSource(explicit=spec)
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="real-browser-user",
        host_session_id="real-browser-parent",
        workspace=str(paths.runtime_workspace_root),
    )
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["reject", "timeout"])
async def test_real_browser_profile_denial_precedes_runtime_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from jiuwenswarm.runtime.harness import external_subagent as subagent_module

    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []

    async def publish(payload: dict, delivery_id: str) -> None:
        published.append((payload, delivery_id))

    admission = ExternalBrowserAdmission(
        parent_subject_id="real-browser-user",
        parent_session_id="real-browser-parent",
        runtime_paths=paths,
        publish=publish,
        timeout_s=0.01 if outcome == "timeout" else 10,
    )
    spec = AgentExecutionSpec("codex", f"r1-10d-profile-{outcome}")
    route = _route(paths, spec)
    factory = ExternalSubagentExecutionFactory(
        route,
        browser_admit=admission,
    )
    child_id = f"real-browser-parent_sub_profile_{outcome}"
    request = SubagentBuildRequest(
        subagent_id=child_id,
        subagent_type="browser_agent",
        display_name="Browser",
        role="Verify Profile admission before runtime startup",
        browser_capabilities=("vision",),
    )
    context = ParentExecutionContext(
        parent_session_id="real-browser-parent",
        parent_subject_id="real-browser-user",
    )

    def unexpected_session_start(*_args, **_kwargs):
        pytest.fail("Profile denial must precede child Session construction")

    monkeypatch.setattr(
        subagent_module,
        "prepare_execution_session",
        unexpected_session_start,
    )
    creation = asyncio.create_task(factory.create(request, context))
    for _ in range(100):
        if published or creation.done():
            break
        await asyncio.sleep(0.01)
    assert len(published) == 1
    payload, delivery_id = published[0]
    assert payload["questions"][0]["tool_name"] == "browser_profile_use"
    assert payload["questions"][0]["tool_payload"] == "[REDACTED]"
    assert payload["request_id"] in delivery_id
    if outcome == "reject":
        assert await admission.answer(
            {
                "request_id": payload["request_id"],
                "answers": [{"selected_options": ["reject"]}],
            }
        )

    with pytest.raises(PermissionError, match="profile use is not allowed"):
        await creation

    assert await admission.answer(
        {
            "request_id": payload["request_id"],
            "answers": [{"selected_options": ["allow_once"]}],
        }
    ) is False
    assert len(route.bindings._bindings) == 1
    assert not (paths.internal_workspace_dir / ".browser-profiles").exists()
    assert not (paths.internal_workspace_dir / "browser-config").exists()
    assert not (paths.runtime_workspace_root / ".browser").exists()
    audit_file = (
        paths.runtime_workspace_root
        / ".jiuwenswarm"
        / "browser-audit"
        / payload["browser_task_id"]
        / "permission_audit"
        / "auto_permission.jsonl"
    )
    records = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["authorization_outcome"] == (
        "deny" if outcome == "reject" else "timeout"
    )
    await admission.close()


@pytest.mark.asyncio
async def test_real_managed_chrome_enforces_admission_and_projects_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_page_server: ThreadingHTTPServer,
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("managed Chrome executable is unavailable")
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", chrome)
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    for name in (
        "PLAYWRIGHT_MCP_COMMAND",
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_CDP_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    server = local_page_server
    paths = _paths(tmp_path)
    child_id = "parent_sub_browser_real"
    binding = ExecutionBinding.create(
        AgentExecutionSpec("codex", "r1-10d-real"),
        subject_id=f"subagent:{child_id}",
        host_session_id=child_id,
        workspace=str(paths.runtime_workspace_root),
    )
    request = SubagentBuildRequest(
        subagent_id=child_id,
        subagent_type="browser_agent",
        display_name="Browser",
        role="Navigate and capture the local acceptance page",
        browser_capabilities=("vision", "pdf"),
    )
    delivered = []
    admitted: list[str] = []

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    def admit(_identity, invocation) -> bool:
        admitted.append(invocation.name)
        return invocation.call_id not in {
            "denied-login-submit",
            "denied-navigate",
            "denied-upload",
        }

    resources = build_external_browser_resources(
        request=request,
        child_binding=binding,
        parent_subject_id="real-browser-user",
        parent_session_id="real-browser-parent",
        channel_id="web",
        runtime_paths=paths,
        admit=admit,
        artifact_sink=sink,
        decision_id_for=(
            lambda _identity, invocation: f"decision-{invocation.call_id}"
        ),
    )
    core_gateway = resources.gateway._gateway
    runtime = core_gateway._runtime
    try:
        definitions = await asyncio.wait_for(resources.gateway.definitions(), 60)
        names = {definition.name for definition in definitions}
        assert {
            "browser_navigate",
            "browser_pdf_save",
            "browser_snapshot",
            "browser_take_screenshot",
        } <= names

        denied_navigation = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "denied-navigate",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/denied"},
                )
            ),
            60,
        )
        assert denied_navigation.is_error is True
        assert _LocalPage.hits == 0

        navigation = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "navigate-call",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/"},
                )
            ),
            60,
        )
        assert navigation.is_error is False, navigation.content.get("result")
        assert _LocalPage.hits >= 1

        task_id = resources.gateway.execution_identity.task.task_id
        task_upload_root = (
            paths.runtime_workspace_root
            / ".jiuwenswarm"
            / "browser-inputs"
            / task_id
        )
        task_upload_root.mkdir(parents=True)
        upload_path = task_upload_root / "upload-fixture.txt"
        upload_path.write_text("R1-10D REAL UPLOAD", encoding="utf-8")
        initial_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        upload_match = re.search(
            r'button "Upload fixture" \[ref=(e\d+)\]',
            initial_snapshot,
        )
        assert upload_match is not None, navigation.content
        open_upload = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "upload-open-call",
                    "browser_click",
                    {
                        "element": "Upload fixture",
                        "target": upload_match.group(1),
                    },
                )
            ),
            60,
        )
        assert open_upload.is_error is False, open_upload.content
        denied_upload = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "denied-upload",
                    "browser_file_upload",
                    {"paths": [str(upload_path)]},
                )
            ),
            60,
        )
        assert denied_upload.is_error is True
        upload = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "upload-call",
                    "browser_file_upload",
                    {"paths": [str(upload_path)]},
                )
            ),
            60,
        )
        assert upload.is_error is False, upload.content
        uploaded_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        assert "upload-fixture.txt:R1-10D REAL UPLOAD" in uploaded_snapshot

        relative_screenshot = (
            Path("outputs") / "browser" / task_id / "screenshots" / "real.png"
        )
        screenshot = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "screenshot-call",
                    "browser_take_screenshot",
                    {
                        "type": "png",
                        "filename": relative_screenshot.as_posix(),
                        "fullPage": True,
                        "scale": "css",
                    },
                )
            ),
            60,
        )
        assert screenshot.is_error is False, screenshot.content
        relative_pdf = Path("outputs") / "browser" / task_id / "real.pdf"
        pdf = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "pdf-call",
                    "browser_pdf_save",
                    {"filename": relative_pdf.as_posix()},
                )
            ),
            60,
        )
        assert pdf.is_error is False, pdf.content
        snapshot_files = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )
        assert snapshot_files
        match = re.search(
            r'link "Download fixture" \[ref=(e\d+)\]',
            snapshot_files[-1].read_text(encoding="utf-8"),
        )
        assert match is not None, navigation.content
        download = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "download-call",
                    "browser_click",
                    {
                        "element": "Download fixture",
                        "target": match.group(1),
                    },
                )
            ),
            60,
        )
        assert download.is_error is False, download.content
        assert admitted == [
            "browser_navigate",
            "browser_navigate",
            "browser_click",
            "browser_file_upload",
            "browser_file_upload",
            "browser_take_screenshot",
            "browser_pdf_save",
            "browser_click",
        ]
        assert len(delivered) == 3, download.content.get("result")
        screenshot_artifact, screenshot_path = delivered[0]
        assert screenshot_path == paths.runtime_workspace_root / relative_screenshot
        assert screenshot_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert screenshot_artifact.metadata["kind"] == "screenshot"
        assert screenshot_artifact.metadata["permission_decision_id"] == (
            "decision-screenshot-call"
        )
        assert screenshot_artifact.metadata["source_url"] == (
            f"http://127.0.0.1:{server.server_port}/"
        )
        pdf_artifact, pdf_path = delivered[1]
        assert pdf_path == paths.runtime_workspace_root / relative_pdf
        assert pdf_path.read_bytes().startswith(b"%PDF-")
        assert pdf_artifact.metadata["kind"] == "pdf"
        assert pdf_artifact.metadata["permission_decision_id"] == "decision-pdf-call"
        download_artifact, download_path = delivered[2]
        assert download_path.parent == (
            paths.runtime_workspace_root / "outputs" / "browser" / task_id
        )
        assert download_path.read_bytes() == b"R1-10D REAL DOWNLOAD"
        assert download_artifact.metadata["kind"] == "download"
        assert download_artifact.metadata["permission_decision_id"] == (
            "decision-download-call"
        )

        login_navigation = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "login-navigation-call",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/login"},
                )
            ),
            60,
        )
        assert login_navigation.is_error is False, login_navigation.content
        login_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        username = re.search(
            r'textbox "Username" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        password = re.search(
            r'textbox "Password" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        sign_in = re.search(
            r'button "Sign in" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        assert username is not None, login_snapshot
        assert password is not None, login_snapshot
        assert sign_in is not None, login_snapshot
        for call_id, element, target, text in (
            ("login-user-call", "Username", username.group(1), "r1-10d-user"),
            ("login-password-call", "Password", password.group(1), "local-secret"),
        ):
            typed = await asyncio.wait_for(
                resources.gateway.invoke(
                    ToolInvocation(
                        call_id,
                        "browser_type",
                        {"element": element, "target": target, "text": text},
                    )
                ),
                60,
            )
            assert typed.is_error is False, typed.content
        hits_before_denied_submit = _LocalPage.hits
        denied_submit = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "denied-login-submit",
                    "browser_click",
                    {"element": "Sign in", "target": sign_in.group(1)},
                )
            ),
            60,
        )
        assert denied_submit.is_error is True
        assert _LocalPage.hits == hits_before_denied_submit
        submit = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "login-submit-call",
                    "browser_click",
                    {"element": "Sign in", "target": sign_in.group(1)},
                )
            ),
            60,
        )
        assert submit.is_error is False, submit.content
        account_navigation = await asyncio.wait_for(
            resources.gateway.invoke(
                ToolInvocation(
                    "account-navigation-call",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/account"},
                )
            ),
            60,
        )
        assert account_navigation.is_error is False, account_navigation.content
        account_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        assert "R1-10D SIGNED IN" in account_snapshot
        assert admitted == [
            "browser_navigate",
            "browser_navigate",
            "browser_click",
            "browser_file_upload",
            "browser_file_upload",
            "browser_take_screenshot",
            "browser_pdf_save",
            "browser_click",
            "browser_navigate",
            "browser_type",
            "browser_type",
            "browser_click",
            "browser_click",
            "browser_navigate",
        ]
    finally:
        await resources.gateway.close()
        await runtime.reset()


@pytest.mark.asyncio
async def test_real_browser_profile_survives_recreation_and_isolates_other_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_page_server: ThreadingHTTPServer,
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("managed Chrome executable is unavailable")
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", chrome)
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    for name in (
        "PLAYWRIGHT_MCP_COMMAND",
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_CDP_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    paths = _paths(tmp_path)
    child_id = "real-browser-parent_sub_profile_recreate"
    request = SubagentBuildRequest(
        subagent_id=child_id,
        subagent_type="browser_agent",
        display_name="Browser",
        role="Retain the authorized login Profile across gateway recreation",
        browser_capabilities=("vision",),
    )

    def build_resources(parent_subject_id: str = "real-browser-user", *, child: str = child_id, parent: str = "real-browser-parent"):
        binding = ExecutionBinding.create(
            AgentExecutionSpec("codex", "r1-10d-profile-recreate"),
            subject_id=f"subagent:{child}",
            host_session_id=child,
            workspace=str(paths.runtime_workspace_root),
        )
        return build_external_browser_resources(
            request=dataclasses.replace(request, subagent_id=child),
            child_binding=binding,
            parent_subject_id=parent_subject_id,
            parent_session_id=parent,
            channel_id="web",
            runtime_paths=paths,
            admit=lambda _identity, _invocation: True,
        )

    server = local_page_server
    first = build_resources()
    first_runtime = first.gateway._runtime
    second = None
    isolated = None
    try:
        navigation = await asyncio.wait_for(
            first.gateway.invoke(
                ToolInvocation(
                    "profile-login-navigation",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/login"},
                )
            ),
            60,
        )
        assert navigation.is_error is False, navigation.content
        task_id = first.gateway.execution_identity.task.task_id
        login_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        username = re.search(
            r'textbox "Username" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        password = re.search(
            r'textbox "Password" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        sign_in = re.search(
            r'button "Sign in" \[ref=([A-Za-z0-9_-]+)\]', login_snapshot
        )
        assert username is not None, login_snapshot
        assert password is not None, login_snapshot
        assert sign_in is not None, login_snapshot
        for call_id, element, target, text in (
            (
                "profile-login-user",
                "Username",
                username.group(1),
                "r1-10d-user",
            ),
            (
                "profile-login-password",
                "Password",
                password.group(1),
                "local-secret",
            ),
        ):
            typed = await asyncio.wait_for(
                first.gateway.invoke(
                    ToolInvocation(
                        call_id,
                        "browser_type",
                        {"element": element, "target": target, "text": text},
                    )
                ),
                60,
            )
            assert typed.is_error is False, typed.content
        submitted = await asyncio.wait_for(
            first.gateway.invoke(
                ToolInvocation(
                    "profile-login-submit",
                    "browser_click",
                    {"element": "Sign in", "target": sign_in.group(1)},
                )
            ),
            60,
        )
        assert submitted.is_error is False, submitted.content

        await first.gateway.close()
        assert first.gateway.closed is True

        second = build_resources(child="second-parent_sub_new-child", parent="second-parent")
        assert second.gateway.execution_identity.profile == first.gateway.execution_identity.profile
        assert second.gateway.execution_identity.instance != first.gateway.execution_identity.instance
        task_id = second.gateway.execution_identity.task.task_id
        account = await asyncio.wait_for(
            second.gateway.invoke(
                ToolInvocation(
                    "profile-account-navigation",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/account"},
                )
            ),
            60,
        )
        assert account.is_error is False, account.content
        account_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        assert "R1-10D SIGNED IN" in account_snapshot

        await second.gateway.close()
        isolated = build_resources("isolated-browser-user")
        assert (
            isolated.gateway.execution_identity.profile.profile_id
            != first.gateway.execution_identity.profile.profile_id
        )
        isolated_account = await asyncio.wait_for(
            isolated.gateway.invoke(
                ToolInvocation(
                    "isolated-profile-account-navigation",
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{server.server_port}/account"},
                )
            ),
            60,
        )
        assert isolated_account.is_error is False, isolated_account.content
        task_id = isolated.gateway.execution_identity.task.task_id
        isolated_snapshot = sorted(
            (paths.runtime_workspace_root / "outputs" / "browser" / task_id).glob(
                "page-*.yml"
            )
        )[-1].read_text(encoding="utf-8")
        assert "R1-10D SIGN IN REQUIRED" in isolated_snapshot
        assert "R1-10D SIGNED IN" not in isolated_snapshot
    finally:
        if isolated is not None:
            await isolated.gateway.close()
            await isolated.gateway._runtime.reset()
        elif second is not None:
            await second.gateway.close()
            await second.gateway._runtime.reset()
        else:
            await first_runtime.reset()


@pytest.mark.asyncio
async def test_real_codex_child_calls_the_same_external_browser_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_page_server: ThreadingHTTPServer,
) -> None:
    from jiuwenswarm.runtime.harness import external_subagent as subagent_module

    sdk = pytest.importorskip(
        "openai_codex",
        reason="optional Codex SDK and bundled CLI required",
    )
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("managed Chrome executable is unavailable")
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", chrome)
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    for name in (
        "PLAYWRIGHT_MCP_COMMAND",
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_CDP_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    paths = _paths(tmp_path)
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    for path in (home, codex_home, codex_home / "skills"):
        path.mkdir()
    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    readable = {
        ":minimal": "read",
        str(paths.runtime_workspace_root): "read",
        str(codex_home / "tmp"): "read",
        str(Path(binary).parent): "read",
    }
    (codex_home / "config.toml").write_text(
        'default_permissions = "r1-10d-read"\n'
        "[permissions.r1-10d-read.filesystem]\n"
        + "\n".join(
            f"{json.dumps(path)} = {json.dumps(access)}"
            for path, access in readable.items()
        )
        + "\n[permissions.r1-10d-read.network]\nenabled=false\n",
        encoding="utf-8",
    )

    page_server = local_page_server
    child_id = "real-browser-parent_sub_browser_agent"
    task_id = "browser-task-" + hashlib.sha256(
        child_id.encode("utf-8")
    ).hexdigest()[:32]
    relative_screenshot = (
        Path("outputs") / "browser" / task_id / "screenshots" / "codex-real.png"
    )
    items = [
        {
            "type": "function_call",
            "namespace": "mcp__jiuwenswarm_product_tools",
            "name": "browser_navigate",
            "id": "fc_browser_navigate",
            "call_id": "call_browser_navigate",
            "arguments": json.dumps(
                {"url": f"http://127.0.0.1:{page_server.server_port}/"}
            ),
        },
        {
            "type": "function_call",
            "namespace": "mcp__jiuwenswarm_product_tools",
            "name": "browser_take_screenshot",
            "id": "fc_browser_screenshot",
            "call_id": "call_browser_screenshot",
            "arguments": json.dumps(
                {
                    "type": "png",
                    "filename": relative_screenshot.as_posix(),
                    "fullPage": True,
                    "scale": "css",
                }
            ),
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_browser_done",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "R1-10D-CODEX-BROWSER-OK",
                    "annotations": [],
                }
            ],
        },
    ]
    captured_resources = []
    gateway_events: list[str] = []
    admitted: list[str] = []
    original_build = subagent_module._build_external_browser_resources

    def capture_resources(**kwargs):
        resources = original_build(**kwargs)
        invoke = resources.gateway.invoke

        async def traced_invoke(invocation):
            gateway_events.append(f"start:{invocation.name}")
            result = await invoke(invocation)
            gateway_events.append(f"done:{invocation.name}:{result.is_error}")
            return result

        resources.gateway.invoke = traced_invoke
        captured_resources.append(resources)
        return resources

    def admit(_identity, invocation) -> bool:
        admitted.append(invocation.name)
        return True

    monkeypatch.setattr(
        subagent_module,
        "_build_external_browser_resources",
        capture_resources,
    )
    delivered = []
    decisions: dict[str, str] = {}

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    def decision_id_for(_identity, invocation) -> str:
        decision_id = f"decision-{invocation.call_id}"
        decisions[invocation.name] = decision_id
        return decision_id

    with _ResponsesFixture(items) as responses:
        spec = AgentExecutionSpec(
            "codex",
            "r1-10d-browser-codex-real",
            authorization=ExecutionAuthorization(full_access=True),
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "cwd": str(paths.cwd),
                "mcp_required": True,
                "mcp_default_tools_approval_mode": "prompt",
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_10d_browser_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        factory = ExternalSubagentExecutionFactory(
            _route(paths, spec),
            browser_admit=admit,
            browser_artifact_sink=sink,
            browser_decision_id_for=decision_id_for,
        )
        execution = await factory.create(
            SubagentBuildRequest(
                subagent_id=child_id,
                subagent_type="browser_agent",
                display_name="Browser",
                role="Use the Browser tools exactly as prescribed",
                browser_capabilities=("vision",),
            ),
            ParentExecutionContext(
                parent_session_id="real-browser-parent",
                parent_subject_id="real-browser-user",
            ),
        )
        results = []

        async def settle(result) -> None:
            results.append(result)

        try:
            try:
                await asyncio.wait_for(
                    execution.run_turn(
                        SubagentTurnRequest(
                            task_id="r1-10d-codex-browser-task",
                            query="Navigate and capture the local acceptance page.",
                        ),
                        on_result=settle,
                    ),
                    timeout=60,
                )
            except TimeoutError:
                pytest.fail(
                    f"Codex Browser call timed out: "
                    f"gateway={gateway_events}, admitted={admitted}, "
                    f"page_hits={_LocalPage.hits}"
                )
        finally:
            await execution.close("test_complete")
            if captured_resources:
                core_gateway = captured_resources[0].gateway._gateway
                await core_gateway._runtime.reset()

    assert len(responses.requests) == 3
    assert len(results) == 1
    assert results[0].output == "R1-10D-CODEX-BROWSER-OK"
    assert results[0].is_error is False
    assert _LocalPage.hits >= 1
    assert admitted == [
        "browser_profile_use",
        "browser_navigate",
        "browser_take_screenshot",
    ]
    assert gateway_events == [
        "start:browser_navigate",
        "done:browser_navigate:False",
        "start:browser_take_screenshot",
        "done:browser_take_screenshot:False",
    ]
    assert len(delivered) == 1
    artifact, artifact_path = delivered[0]
    assert artifact_path == paths.runtime_workspace_root / relative_screenshot
    assert artifact.metadata["permission_decision_id"] == decisions[
        "browser_take_screenshot"
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_OPENCODE_OC3") != "1",
    reason="real managed OpenCode acceptance is separately opt-in",
)
async def test_real_opencode_child_calls_the_same_external_browser_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_page_server: ThreadingHTTPServer,
) -> None:
    from jiuwenswarm.runtime.harness import external_subagent as subagent_module

    cli = Path(
        os.environ.get(
            "OPENCODE_OC1_CLI",
            os.path.expanduser("~/.opencode/bin/opencode"),
        )
    )
    if not cli.is_file() or not os.access(cli, os.X_OK):
        pytest.skip("managed OpenCode executable is unavailable")
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("managed Chrome executable is unavailable")
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", chrome)
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    for name in (
        "PLAYWRIGHT_MCP_COMMAND",
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_CDP_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    paths = _paths(tmp_path)
    runtime_root = (tmp_path / "opencode-runtime").resolve()
    runtime_root.mkdir(mode=0o700)
    page_server = local_page_server
    child_id = "real-browser-parent_sub_browser_opencode"
    task_id = "browser-task-" + hashlib.sha256(
        child_id.encode("utf-8")
    ).hexdigest()[:32]
    relative_screenshot = (
        Path("outputs") / "browser" / task_id / "screenshots" / "opencode-real.png"
    )
    model = _ChatCompletionsFixture(
        [
            {
                "tool": "jiuwenswarm_product_tools_browser_navigate",
                "args": {"url": f"http://127.0.0.1:{page_server.server_port}/"},
            },
            {
                "tool": "jiuwenswarm_product_tools_browser_take_screenshot",
                "args": {
                    "type": "png",
                    "filename": relative_screenshot.as_posix(),
                    "fullPage": True,
                    "scale": "css",
                },
            },
            {"text": "R1-10D-OPENCODE-BROWSER-OK"},
        ]
    )
    app = web.Application()
    app.router.add_post("/v1/chat/completions", model.respond)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()

    captured_resources = []
    admitted: list[str] = []
    delivered = []
    decisions: dict[str, str] = {}
    original_build = subagent_module._build_external_browser_resources

    def capture_resources(**kwargs):
        resources = original_build(**kwargs)
        captured_resources.append(resources)
        return resources

    def admit(_identity, invocation) -> bool:
        admitted.append(invocation.name)
        return True

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    def decision_id_for(_identity, invocation) -> str:
        decision_id = f"decision-{invocation.call_id}"
        decisions[invocation.name] = decision_id
        return decision_id

    monkeypatch.setattr(
        subagent_module,
        "_build_external_browser_resources",
        capture_resources,
    )
    spec = AgentExecutionSpec(
        "opencode",
        "r1-10d-browser-opencode-real",
        authorization=ExecutionAuthorization(full_access=True),
        provider_config={
            "cli_path": str(cli),
            "runtime_root": str(runtime_root),
            "model": {
                "model": "fixture",
                "api_base": f"http://127.0.0.1:{port}/v1",
                "api_key": "fixture-only",
            },
            "turn_timeout_s": 45,
        },
    )
    factory = ExternalSubagentExecutionFactory(
        _route(paths, spec),
        browser_admit=admit,
        browser_artifact_sink=sink,
        browser_decision_id_for=decision_id_for,
    )
    execution = None
    results = []

    async def settle(result) -> None:
        results.append(result)

    try:
        execution = await factory.create(
            SubagentBuildRequest(
                subagent_id=child_id,
                subagent_type="browser_agent",
                display_name="Browser",
                role="Use the Browser tools exactly as prescribed",
                browser_capabilities=("vision",),
            ),
            ParentExecutionContext(
                parent_session_id="real-browser-parent",
                parent_subject_id="real-browser-user",
            ),
        )
        await asyncio.wait_for(
            execution.run_turn(
                SubagentTurnRequest(
                    task_id="r1-10d-opencode-browser-task",
                    query="Navigate and capture the local acceptance page.",
                ),
                on_result=settle,
            ),
            timeout=60,
        )
    finally:
        if execution is not None:
            await execution.close("test_complete")
        if captured_resources:
            core_gateway = captured_resources[0].gateway._gateway
            await core_gateway._runtime.reset()
        await runner.cleanup()
        for directory in runtime_root.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)

    assert len(model.requests) == 3
    assert len(results) == 1
    assert results[0].output == "R1-10D-OPENCODE-BROWSER-OK"
    assert results[0].is_error is False
    assert _LocalPage.hits >= 1
    assert admitted == [
        "browser_profile_use",
        "browser_navigate",
        "browser_take_screenshot",
    ]
    assert len(delivered) == 1
    artifact, artifact_path = delivered[0]
    assert artifact_path == paths.runtime_workspace_root / relative_screenshot
    assert artifact_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert artifact.metadata["permission_decision_id"] == decisions[
        "browser_take_screenshot"
    ]


@pytest.mark.asyncio
async def test_real_download_cancel_stops_old_writer_before_profile_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    local_page_server: ThreadingHTTPServer,
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("managed Chrome executable is unavailable")
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", chrome)
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    for name in ("PLAYWRIGHT_MCP_COMMAND", "PLAYWRIGHT_MCP_ARGS", "PLAYWRIGHT_MCP_CDP_ENDPOINT", "PLAYWRIGHT_CDP_URL"):
        monkeypatch.delenv(name, raising=False)
    paths = _paths(tmp_path)
    delivered = []

    async def sink(artifact, path):
        delivered.append((artifact, path))

    def build(child):
        return build_external_browser_resources(
            request=SubagentBuildRequest(
                subagent_id=child, subagent_type="browser_agent", display_name="Browser",
                role="Verify slow download cancellation", browser_capabilities=("vision",),
            ),
            child_binding=ExecutionBinding.create(
                AgentExecutionSpec("codex", "download-cancel"),
                subject_id=f"subagent:{child}", host_session_id=child,
                workspace=str(paths.runtime_workspace_root),
            ),
            parent_subject_id="download-owner", parent_session_id="download-parent", channel_id="web",
            runtime_paths=paths, admit=lambda *_args: True, artifact_sink=sink,
            decision_id_for=lambda *_args: "download-approval",
        )

    first = build("download-parent_sub_first")
    second = None
    download = None
    try:
        url = f"http://127.0.0.1:{local_page_server.server_port}/"
        result = await asyncio.wait_for(first.gateway.invoke(ToolInvocation("navigate", "browser_navigate", {"url": url})), 60)
        assert not result.is_error, result.content
        runtime = first.gateway._gateway._runtime
        process = runtime.service._managed_driver._process
        task_id = first.gateway.execution_identity.task.task_id
        output = paths.outputs_dir / "browser" / task_id
        snapshot = sorted(output.glob("page-*.yml"))[-1].read_text()
        target = re.search(r'link "Slow download fixture" \[ref=(\w+)\]', snapshot)
        assert target is not None, snapshot
        download = asyncio.create_task(first.gateway.invoke(ToolInvocation(
            "slow", "browser_click", {"element": "Slow download fixture", "target": target.group(1)},
        )))
        state = first.gateway._download_state_root
        for _ in range(200):
            if list(state.glob("*.pending")) and list(output.rglob("*.crdownload")):
                break
            if download.done():
                pytest.fail(f"download completed before cancellation: {download.result()}")
            await asyncio.sleep(0.05)
        assert list(state.glob("*.pending"))
        assert list(output.rglob("*.crdownload")), "must observe a real active file writer"
        download.cancel()
        with pytest.raises(asyncio.CancelledError):
            await download
        await asyncio.wait_for(first.gateway.close(), 30)
        assert process.poll() is not None, "close must confirm Chrome exit"
        assert not list(state.glob("*.pending"))
        assert not delivered, "partial download must never become an Artifact"
        sizes = {str(p): p.stat().st_size for p in output.rglob("*") if p.is_file()}
        await asyncio.sleep(0.3)
        assert sizes == {str(p): p.stat().st_size for p in output.rglob("*") if p.is_file()}
        second = build("download-parent_sub_second")
        assert second.gateway.execution_identity.profile == first.gateway.execution_identity.profile
        result = await asyncio.wait_for(second.gateway.invoke(ToolInvocation("new", "browser_navigate", {"url": url})), 60)
        assert not result.is_error, result.content
        assert not list(second.gateway._download_state_root.glob("*.pending"))
    finally:
        if download is not None and not download.done():
            download.cancel()
            await asyncio.gather(download, return_exceptions=True)
        if second is not None:
            await second.gateway.close()
        await first.gateway.close()
