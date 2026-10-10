#!/usr/bin/env python3
"""Run the real split Web/Gateway/AgentServer stack with isolated demo data."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def process_matches(record):
    try:
        raw = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\0")
        return record["module"].encode() in raw and str(ROOT).encode() in raw[0]
    except (OSError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".taskboard-demo")
    parser.add_argument(
        "--base-port",
        type=int,
        default=19240,
        help="Web, Gateway, AgentServer and auxiliary gateway use this port and +1/+2/+3",
    )
    args = parser.parse_args()
    data = args.data_dir.resolve()
    manifest = data / "processes.json"
    records = json.loads(manifest.read_text()) if manifest.exists() else {}
    if args.action in {"stop", "status"}:
        for name, record in records.items():
            alive = process_matches(record)
            print(f"{name}: {'running' if alive else 'stopped'}")
            if args.action == "stop" and alive:
                os.kill(record["pid"], signal.SIGTERM)
        if args.action == "stop":
            deadline = time.monotonic() + 15
            while (
                any(process_matches(r) for r in records.values())
                and time.monotonic() < deadline
            ):
                time.sleep(0.2)
            if any(process_matches(r) for r in records.values()):
                parser.error(
                    "owned demo processes have not stopped; inspect logs before retrying"
                )
        return
    if any(process_matches(r) for r in records.values()):
        parser.error("demo processes already running; stop them first")
    if not 1024 <= args.base_port <= 65532:
        parser.error("base port must be 1024..65532")
    for port in range(args.base_port, args.base_port + 4):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                parser.error(f"port {port} is in use")
    config_dir = data / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    if not config_path.exists():
        import yaml

        cfg = yaml.safe_load((ROOT / "jiuwenswarm/resources/config.yaml").read_text())
        cfg.update(
            taskboard={"enabled": True},
            setup_guide={"enabled": False},
            heartbeat={"enabled": False},
            cron={"enabled": False},
        )
        # The distributed config contains placeholder model endpoints. A manual
        # Taskboard demo needs no Provider and must not probe those endpoints.
        cfg["models"]["defaults"] = []
        cfg.setdefault("react", {})["enable_read_image_multimodal"] = False
        config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        config_path.chmod(0o600)
    dist = ROOT / "jiuwenswarm/channels/web/frontend/dist"
    if not (dist / "index.html").exists():
        parser.error("frontend dist missing; run npm run build first")
    env = os.environ.copy()
    env.update(
        JIUWENSWARM_DATA_DIR=str(data),
        JIUWENSWARM_CONFIG_DIR=str(config_dir),
        AGENT_SERVER_HOST="127.0.0.1",
        AGENT_SERVER_PORT=str(args.base_port + 2),
        GATEWAY_HOST="127.0.0.1",
        GATEWAY_PORT=str(args.base_port + 3),
        WEB_HOST="127.0.0.1",
        WEB_PORT=str(args.base_port + 1),
        FRONTEND_PORT=str(args.base_port),
        GATEWAY_URL=f"http://127.0.0.1:{args.base_port + 1}",
        PYTHONUNBUFFERED="1",
    )
    env.pop("PYTHONPATH", None)
    modules = [
        (
            "agent",
            "jiuwenswarm.server.app_agentserver",
            ["--port", str(args.base_port + 2)],
        ),
        (
            "gateway",
            "jiuwenswarm.gateway.app_gateway",
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(args.base_port + 1),
                "--agent-server-url",
                f"ws://127.0.0.1:{args.base_port + 2}",
            ],
        ),
        (
            "web",
            "jiuwenswarm.channels.web.app_web",
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(args.base_port),
                "--dist",
                str(dist),
                "--proxy-target",
                f"http://127.0.0.1:{args.base_port + 1}",
            ],
        ),
    ]
    records = {}
    try:
        for name, module, extra in modules:
            with (data / f"{name}.log").open("ab") as log:
                proc = subprocess.Popen(
                    [str(ROOT / ".venv/bin/python"), "-m", module, *extra],
                    cwd=data,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            records[name] = {"pid": proc.pid, "module": module}
            manifest.write_text(json.dumps(records, indent=2))
    except BaseException:
        for record in records.values():
            if process_matches(record):
                os.kill(record["pid"], signal.SIGTERM)
        raise
    print(f"Demo starting: http://127.0.0.1:{args.base_port}/taskboard")
    print(f"Data and logs: {data}")
    print(
        "First startup can take a minute. Use status and inspect agent.log/gateway.log if unavailable."
    )


if __name__ == "__main__":
    main()
