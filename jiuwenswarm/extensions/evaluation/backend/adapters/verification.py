"""M1 shared-host verification of an explicitly authorized personal task.

This is not an OS sandbox or independent evaluation. No secrets from the model
process are inherited by the test process. Reports remain in the original trial
workspace; the metadata store contains only the result and relative evidence refs.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import time

from .pipeline import core_result
from .store import CatalogError

MAX_OUTPUT = 1024 * 1024


def workspace_file(root: Path, relative: str) -> Path:
    from ..models import relative_path

    relative_path(relative)
    target = root / relative
    for current in [root, *target.relative_to(root).parents]:
        if current == root:
            candidate = root
        else:
            candidate = root / current
        if candidate.is_symlink():
            raise CatalogError("UNSAFE_EVIDENCE_PATH")
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise CatalogError("UNSAFE_EVIDENCE_PATH")
    return target


class SharedHostVerifier:
    def __init__(self):
        self.processes = {}

    async def cancel(self, attempt_id):
        process = self.processes.get(attempt_id)
        if process is not None:
            await self._stop(process)
            self.processes.pop(attempt_id, None)

    @staticmethod
    async def _stop(process):
        # The session/group was created by this verifier, never discovered by name.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), timeout=5)
        # A direct child's return code is insufficient when it spawned children.
        deadline = time.monotonic() + 5
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            if sys.platform.startswith("linux"):
                live = False
                for stat in Path("/proc").glob("[0-9]*/stat"):
                    try:
                        fields = stat.read_text().rsplit(")", 1)[1].split()
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        continue
                    if int(fields[2]) == process.pid and fields[0] != "Z":
                        live = True
                        break
                if not live:
                    return
            if time.monotonic() >= deadline:
                raise CatalogError("EXIT_NOT_CONFIRMED")
            await asyncio.sleep(0.05)

    async def verify(self, attempt_id, workspace, task):
        if task.acceptance.kind == "manual":
            return {
                "outcome": "awaiting_manual_review",
                "exit_confirmed": True,
                "usage": None,
            }
        if os.name != "posix":
            raise CatalogError("SHARED_HOST_UNSUPPORTED")
        report_root = workspace / ".evaluation"
        if report_root.is_symlink():
            raise CatalogError("UNSAFE_EVIDENCE_PATH")
        report_root.mkdir(mode=0o700, exist_ok=True)
        script = report_root / "acceptance.py"
        output = report_root / "test-output.txt"
        if script.is_symlink() or output.is_symlink():
            raise CatalogError("UNSAFE_EVIDENCE_PATH")
        script.write_text(task.acceptance.script, encoding="utf-8")
        # Keep the interpreter isolated from user-site packages but explicitly use
        # the same task cwd for imports. Syntax/environment failures differ from assertions.
        wrapper = """import pathlib,sys,traceback
sys.path.insert(0, str(pathlib.Path.cwd()))
try:
    source=pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    exec(compile(source, sys.argv[1], "exec"), {"__name__":"__main__"})
except AssertionError:
    traceback.print_exc(); sys.exit(1)
except SystemExit as exc:
    sys.exit(exc.code)
except BaseException:
    traceback.print_exc(); sys.exit(2)
"""
        started = time.monotonic()
        env = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "HOME": str(report_root),
            "TMPDIR": str(report_root),
        }
        with output.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                wrapper,
                str(script),
                cwd=workspace,
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self.processes[attempt_id] = process
            outcome = None
            try:
                deadline = started + task.acceptance.timeout_seconds
                while process.returncode is None:
                    if time.monotonic() >= deadline:
                        outcome = "verification_timeout"
                        break
                    if output.stat().st_size > MAX_OUTPUT:
                        outcome = "environment_error"
                        break
                    await asyncio.sleep(0.05)
                if outcome is not None:
                    await self._stop(process)
                else:
                    await process.wait()
            finally:
                # Reap the owned group even when the direct child exited first.
                await self._stop(process)
                self.processes.pop(attempt_id, None)
        with output.open("rb") as log:
            text = log.read(MAX_OUTPUT).decode("utf-8", errors="replace")
        result = core_result(returncode=process.returncode, stdout=text, stderr="")
        if outcome is None:
            outcome = (
                "passed"
                if result.passed
                else "test_failed"
                if process.returncode == 1
                else "environment_error"
            )
        return {
            "outcome": outcome,
            "exit_confirmed": True,
            "returncode": result.returncode,
            "test_output": result.test_output,
            "test_output_truncated": output.stat().st_size > MAX_OUTPUT,
            "pass_rate": result.pass_rate,
            "verification_seconds": time.monotonic() - started,
            "acceptance_policy": "shared-environment-v1",
        }
