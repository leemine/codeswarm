# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identity of a trusted local, single-user AgentServer installation.

This identifies the installation, not an authenticated remote human. A remote
or multi-user composition root must supply its own authenticated resolver.
Neither routing IDs nor request payloads participate in this identity.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path

from jiuwenswarm.governance.contracts import TrustedIdentity


def local_instance_identity(host: str, data_root: Path) -> TrustedIdentity | None:
    """Freeze local installation ownership only for an explicit loopback bind."""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        # Do not resolve a hostname and silently turn a remote deployment into
        # a trusted local instance. The default server uses 127.0.0.1.
        return None
    root = data_root.resolve()
    if hasattr(os, "getuid"):
        principal = str(os.getuid())
    else:
        # The per-user data root is the existing installation boundary on
        # Windows; no environment-supplied username is treated as proof.
        principal = str(Path.home().resolve())
    digest = hashlib.sha256(f"{principal}\0{root}".encode()).hexdigest()
    identity = f"local-instance:{digest}"
    return TrustedIdentity(identity, identity, "local-single-user-installation")
