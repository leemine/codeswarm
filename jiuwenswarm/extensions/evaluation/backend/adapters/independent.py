"""Personal Native delivery policy over the existing core Docker environment.

The execution workspace is never mounted. Only a fixed baseline plus declared
files enter /work; authority and offline wheels are separate read-only mounts.
This is reproducibility isolation, not a hostile-host security boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import time
import uuid

from openjiuwen.agent_evolving.evaluator.evaluator_pipeline import (
    DockerEnvironment,
    ExecResult,
)

from .deliverables import capture, read_delivery
from .store import CatalogError
from .verification import MAX_OUTPUT

POLICY = "independent-container-v1"
REPORT = "EVALUATION_AUTHORITY_REPORT="
RUNNER = """import ast,json,pathlib,sys,traceback
sys.path[:0]=['/work','/deps']
count=[0]
def checked(): count[0]+=1
class Instrument(ast.NodeTransformer):
 def visit_Assert(self,node):
  return [ast.copy_location(ast.Expr(ast.Call(ast.Name('__evaluation_check__',ast.Load()),[],[])),node),node]
code=2
try:
 tree=ast.parse(pathlib.Path('/authority/test.py').read_text())
 tree=ast.fix_missing_locations(Instrument().visit(tree))
 exec(compile(tree,'/authority/test.py','exec'),{'__name__':'__main__','__evaluation_check__':checked})
 code=0 if count[0]>0 else 2
 if count[0]==0: print('No authoritative assertion executed')
except AssertionError:
 traceback.print_exc();code=1
except BaseException:
 traceback.print_exc();code=2
print('EVALUATION_AUTHORITY_REPORT='+json.dumps({'assertions':count[0],'returncode':code}),flush=True)
sys.exit(code)
"""


async def command(args, *, timeout=30):
    """Bound all Docker client output and always reap this client's process."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    exceeded = False

    async def read(stream):
        nonlocal exceeded
        chunks, size = [], 0
        while data := await stream.read(65536):
            available = max(0, MAX_OUTPUT - size)
            chunks.append(data[:available])
            size += len(data)
            if size > MAX_OUTPUT:
                exceeded = True
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    readers = [
        asyncio.create_task(read(proc.stdout)),
        asyncio.create_task(read(proc.stderr)),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        values = await asyncio.gather(*readers)
    return ExecResult(
        stdout=values[0],
        stderr=values[1]
        + ("\nOUTPUT_LIMIT" if exceeded else "")
        + ("\nCommand timed out" if timed_out else ""),
        returncode=-1 if timed_out or exceeded else proc.returncode,
        timed_out=timed_out,
    )


def docker():
    value = shutil.which("docker")
    if value is None:
        raise CatalogError("VERIFIER_DOCKER_UNAVAILABLE")
    return value


async def environment_snapshot():
    image = os.environ.get("JIUWENSWARM_EVALUATION_IMAGE", "python:3.12-slim")
    result = await command([docker(), "image", "inspect", image, "--format", "{{.Id}}"])
    if result.returncode or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", result.stdout.strip()
    ):
        raise CatalogError("VERIFIER_IMAGE_UNAVAILABLE")
    wheels = []
    root = os.environ.get("JIUWENSWARM_EVALUATION_WHEELHOUSE")
    if root:
        directory = Path(root)
        if directory.is_symlink() or not directory.is_dir():
            raise CatalogError("INVALID_WHEELHOUSE")
        for path in sorted(directory.glob("*.whl")):
            data = read_delivery(directory, path.name)
            if data is None:
                raise CatalogError("INVALID_WHEELHOUSE")
            wheels.append(
                {"name": path.name, "sha256": hashlib.sha256(data[0]).hexdigest()}
            )
        if len(wheels) > 100:
            raise CatalogError("WHEELHOUSE_TOO_LARGE")
    return {
        "policy": POLICY,
        "image_id": result.stdout.strip(),
        "wheels": wheels,
        "network": "none",
        "cpus": 1,
        "memory_mb": 512,
        "pids_limit": 128,
        "dependency_strategy": "offline-wheels-require-hashes-v1",
        "authority_runner_sha256": hashlib.sha256(RUNNER.encode()).hexdigest(),
    }


class VerificationEnvironment(DockerEnvironment):
    """Specialize launch/cleanup policy; reuse core exec/copy and result contracts."""

    def __init__(self, snapshot, authority, wheels, materials=None):
        self.owner = uuid.uuid4().hex
        super().__init__(
            snapshot["image_id"], container_name="evaluation-" + self.owner
        )
        self.authority, self.wheels = authority, wheels
        self.materials = materials
        self.lock = asyncio.Lock()
        self.cancelled = False
        self.removed = False

    async def _run_command(self, cmd, timeout=300):
        return await command(cmd, timeout=timeout)

    async def start(self):
        async with self.lock:
            if self.cancelled:
                raise CatalogError("VERIFICATION_CANCELLED")
            # Reserve our name before awaiting; stop checks its random ownership label.
            self._container_id = self.container_name
            args = [
                docker(),
                "create",
                "--rm",
                "--name",
                self.container_name,
                "--label",
                "workswarm.evaluation.owner=" + self.owner,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/work:rw,nosuid,nodev,size=32m,mode=1777",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
                "--tmpfs",
                "/deps:rw,nosuid,nodev,size=64m,mode=1777",
                "--mount",
                f"type=bind,source={self.authority},target=/authority,readonly",
                "--mount",
                f"type=bind,source={self.wheels},target=/wheels,readonly",
                "--mount",
                f"type=bind,source={self.materials},target=/input,readonly",
                "--env",
                "HOME=/tmp",
                "--env",
                "LANG=C.UTF-8",
                self.image_tag,
                "sleep",
                "600",
            ]
            result = await command(args)
            if result.returncode:
                raise CatalogError("VERIFIER_CREATE_FAILED")
            result = await command([docker(), "start", self.container_name])
            if result.returncode:
                raise CatalogError("VERIFIER_START_FAILED")

    async def stop(self):
        async with self.lock:
            self.cancelled = True
            if self._container_id is None:
                return
            name = self._container_id
            result = await command(
                [docker(), "inspect", name, "--format", "{{json .Config.Labels}}"]
            )
            if result.returncode:
                if "no such" not in result.stderr.lower():
                    raise CatalogError("EXIT_NOT_CONFIRMED")
            else:
                if (
                    json.loads(result.stdout).get("workswarm.evaluation.owner")
                    != self.owner
                ):
                    raise CatalogError("EXIT_NOT_CONFIRMED")
                removed = await command([docker(), "rm", "-f", name])
                check = await command([docker(), "inspect", name])
                if (
                    removed.returncode
                    or check.returncode == 0
                    or "no such" not in check.stderr.lower()
                ):
                    raise CatalogError("EXIT_NOT_CONFIRMED")
            self.removed = True
            self._container_id = None


LOCK_LINE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)\s+--hash=sha256:([a-f0-9]{64})$"
)


def dependency_materials(task, work, wheel_dir, snapshot):
    if not task.acceptance.dependency_lock:
        return None
    lock = work / task.acceptance.dependency_lock
    if not lock.is_file() or lock.stat().st_size > 65536:
        raise CatalogError("DEPENDENCY_LOCK_MISSING")
    lines = [
        line.strip()
        for line in lock.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or len(lines) > 100:
        raise CatalogError("INVALID_DEPENDENCY_LOCK")
    available = {item["sha256"]: item for item in snapshot["wheels"]}
    names = set()
    for line in lines:
        match = LOCK_LINE.fullmatch(line)
        if not match or match[1].lower() in names:
            raise CatalogError("INVALID_DEPENDENCY_LOCK")
        names.add(match[1].lower())
        wheel = available.get(match[3])
        if wheel is None:
            raise CatalogError("DECLARED_DEPENDENCY_UNAVAILABLE")
        value = read_delivery(
            Path(os.environ["JIUWENSWARM_EVALUATION_WHEELHOUSE"]), wheel["name"]
        )
        if value is None or hashlib.sha256(value[0]).hexdigest() != wheel["sha256"]:
            raise CatalogError("VERIFIER_ENVIRONMENT_CHANGED")
        target = wheel_dir / wheel["name"]
        target.write_bytes(value[0])
        target.chmod(0o644)
    return {
        "path": task.acceptance.dependency_lock,
        "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "locked_requirements": len(lines),
    }


class IndependentVerifier:
    def __init__(self, root):
        self.root = root
        self.environments = {}
        self.cancelled = set()

    async def cancel(self, attempt_id, recovery=None):
        self.cancelled.add(attempt_id)
        environment = self.environments.get(attempt_id)
        if environment is None and recovery:
            token = recovery.get("owner", "")
            if (
                not re.fullmatch(r"[a-f0-9]{32}", token)
                or recovery.get("name") != "evaluation-" + token
            ):
                raise CatalogError("EXIT_NOT_CONFIRMED")
            environment = VerificationEnvironment(
                {"image_id": ""}, self.root, self.root
            )
            environment.owner = token
            environment._container_name = recovery["name"]
            environment._container_id = recovery["name"]
            self.environments[attempt_id] = environment
        if environment is not None:
            await environment.stop()
            self.environments.pop(attempt_id, None)

    async def verify(self, attempt_id, workspace, task, snapshot, *, remember=None):
        started = time.monotonic()
        if task.acceptance.kind == "manual":
            return {
                "outcome": "awaiting_manual_review",
                "acceptance_policy": POLICY,
                "exit_confirmed": True,
            }
        if await environment_snapshot() != snapshot:
            raise CatalogError("VERIFIER_ENVIRONMENT_CHANGED")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        result = {
            "acceptance_policy": POLICY,
            "verification_environment": snapshot,
            "authority_sha256": hashlib.sha256(
                task.acceptance.script.encode()
            ).hexdigest(),
        }
        environment = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="verify-", dir=self.root
            ) as directory:
                stage = Path(directory)
                work, authority, wheels = (
                    stage / name for name in ("work", "authority", "wheels")
                )
                manifest = capture(workspace, task, work)
                result["delivery_manifest"] = manifest
                authority.mkdir(mode=0o755)
                wheels.mkdir(mode=0o755)
                result["dependencies"] = dependency_materials(
                    task, work, wheels, snapshot
                )
                for name, source in [
                    ("test.py", task.acceptance.script),
                    ("runner.py", RUNNER),
                ]:
                    (authority / name).write_text(source)
                    (authority / name).chmod(0o644)
                if attempt_id in self.cancelled:
                    return {**result, "outcome": "cancelled", "exit_confirmed": True}
                work.chmod(0o755)
                environment = VerificationEnvironment(snapshot, authority, wheels, work)
                self.environments[attempt_id] = environment
                result["verifier_container"] = environment.container_name
                ownership = {
                    "name": environment.container_name,
                    "owner": environment.owner,
                }
                result["verifier_ownership"] = ownership
                if remember is not None:
                    remember(ownership)
                try:
                    await environment.start()
                    copy_script = """import pathlib,shutil
source=pathlib.Path('/input')
for path in source.rglob('*'):
 target=pathlib.Path('/work')/path.relative_to(source)
 if path.is_dir(): target.mkdir(parents=True,exist_ok=True)
 else: shutil.copy2(path,target)
"""
                    copied = await environment.exec(
                        "python -I -c " + shlex.quote(copy_script)
                    )
                    if copied.returncode:
                        raise CatalogError("VERIFIER_COPY_FAILED")
                    if result["dependencies"]:
                        lock = shlex.quote("/work/" + task.acceptance.dependency_lock)
                        installed = await environment.exec(
                            "python -I -m pip install --disable-pip-version-check --no-index --only-binary=:all: --require-hashes --find-links=/wheels --target=/deps -r "
                            + lock,
                            timeout=60,
                            workdir="/work",
                        )
                        result["dependency_output"] = (
                            installed.stdout + installed.stderr
                        )
                        if installed.returncode:
                            raise CatalogError("DEPENDENCY_INSTALL_FAILED")
                    execution = await environment.exec(
                        "python -I /authority/runner.py",
                        timeout=task.acceptance.timeout_seconds,
                        workdir="/work",
                    )
                    output = execution.stdout + execution.stderr
                    reports = [
                        line[len(REPORT) :]
                        for line in execution.stdout.splitlines()
                        if line.startswith(REPORT)
                    ]
                    try:
                        report = json.loads(reports[0]) if len(reports) == 1 else {}
                    except ValueError:
                        report = {}
                    valid = (
                        type(report.get("assertions")) is int
                        and report["assertions"] > 0
                        and report.get("returncode") == execution.returncode
                    )
                    outcome = (
                        "verification_timeout"
                        if execution.timed_out
                        else "passed"
                        if execution.returncode == 0 and valid
                        else "test_failed"
                        if execution.returncode == 1 and valid
                        else "environment_error"
                    )
                    result.update(
                        outcome=outcome,
                        returncode=execution.returncode,
                        test_output=output,
                        authoritative_assertions=report.get("assertions", 0),
                    )
                finally:
                    await environment.stop()
                    result["verifier_removed"] = environment.removed
        except CatalogError as exc:
            if exc.code == "EXIT_NOT_CONFIRMED":
                raise
            result.update(
                outcome="artifact_error"
                if exc.code
                in {
                    "INVALID_DELIVERY_FILE",
                    "DELIVERY_TOO_LARGE",
                    "UNSAFE_DELIVERY_PATH",
                    "MISSING_DELIVERY",
                    "DELIVERY_CHANGED_DURING_EXPORT",
                }
                else "environment_error",
                error_code=exc.code,
            )
        finally:
            if environment is not None and environment.removed:
                self.environments.pop(attempt_id, None)
        if attempt_id in self.cancelled:
            result["outcome"] = "cancelled"
            self.cancelled.discard(attempt_id)
        return {
            **result,
            "exit_confirmed": True,
            "verification_seconds": time.monotonic() - started,
        }
