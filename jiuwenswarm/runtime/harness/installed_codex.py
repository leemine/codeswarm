"""Materialize the personal Codex recipe inside an admitted Session scope.

The menu fingerprint identifies the recipe. The original execution Binding and
recovery archive pin its concrete paths; no Provider state or credentials are
stored in Session metadata.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat

from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource


REVISION = "installed-codex-v2"
PROFILE = "builtin:codex"


def materialize_codex_source(source, *, profile_id, paths, subject_id, session_id):
    spec = source.resolve()
    if profile_id != PROFILE or spec.provider_id != "codex" or spec.config_revision != REVISION:
        return source
    # This function is called only after product Workspace admission. Never use
    # a global root, a client-supplied provider_config or an unbound project.
    workspace = paths.runtime_workspace_root.resolve()
    scope = hashlib.sha256(json.dumps([subject_id, session_id, str(workspace)]).encode()).hexdigest()
    root = paths.internal_workspace_dir.resolve() / "codex-runtime" / scope
    home, codex_home = root / "home", root / "codex"
    if any(workspace.is_relative_to(p) or p.is_relative_to(workspace) for p in (home, codex_home)):
        raise ValueError("Codex configuration homes must be separate from the selected workspace")
    config = dict(spec.provider_config)
    config.update({
        "inherit_process_env": False,
        # Codex materializes its bundled skills in its isolated home. Admit
        # that exact Session-owned tree as well, never the user's CLI home.
        "startup_source_roots": [str(workspace), str(home), str(codex_home)],
        "env": {**config.get("env", {}), "HOME": str(home), "CODEX_HOME": str(codex_home)},
    })
    return ExecutionConfigSource(explicit=replace(spec, provider_config=config))


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Codex runtime directories must be privately owned, non-symlink directories (0700)")


def prepare_codex_startup(route) -> None:
    """Allocate only after admission; reuse login without importing CLI config.

    Existing isolated auth may have been refreshed by Codex. Do not overwrite
    it with an older host token. Login files never enter source/recovery output.
    """
    spec = route.bound.spec
    if (route.recovery is None or route.recovery.execution_profile_id != PROFILE
            or spec.provider_id != "codex" or spec.config_revision != REVISION):
        return
    env = spec.provider_config["env"]
    home, codex_home = Path(env["HOME"]), Path(env["CODEX_HOME"])
    base = route.runtime_paths.internal_workspace_dir.resolve() / "codex-runtime"
    if home.parent != codex_home.parent or home.parent.parent != base:
        raise ValueError("Codex runtime homes do not match the admitted scope")
    for directory in (base, home.parent, home, codex_home):
        _private_directory(directory)
    # The locked adapter confirms a named permission profile when host review
    # is enabled. Define it in the isolated server config (not a per-thread
    # overlay), so config/read can verify provenance and subsequent switches.
    # Tool processes get only the admitted workspace plus minimal system files;
    # the login/config homes are not writable/readable tool workspace roots.
    configuration = (
        'default_permissions = "codeswarm-session"\n'
        '[permissions.codeswarm-session.filesystem]\n'
        '":minimal" = "read"\n'
        f'{json.dumps(str(route.runtime_paths.runtime_workspace_root.resolve()))} = "write"\n'
        '[permissions.codeswarm-session.network]\nenabled = false\n'
    ).encode()
    config_path = codex_home / "config.toml"
    if config_path.exists() or config_path.is_symlink():
        info = config_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or config_path.read_bytes() != configuration):
            raise ValueError("Codex isolated permission configuration changed; create a new session")
    else:
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(configuration)
    target = codex_home / "auth.json"
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Codex isolated login must be a private regular file")
        return
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    try:
        with source.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Codex login source must be an owner-controlled regular file")
            data = stream.read(1024 * 1024 + 1)
    except FileNotFoundError as exc:
        raise RuntimeError("Codex login is unavailable; sign in with codex login, then start a new session") from exc
    try:
        value = json.loads(data)
        valid = len(data) <= 1024 * 1024 and isinstance(value, dict) and (
            isinstance(value.get("tokens"), dict) or bool(value.get("OPENAI_API_KEY")))
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise RuntimeError("Codex login is invalid; sign in with codex login, then start a new session")
    # Exclusive creation prevents following a concurrently replaced destination.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
