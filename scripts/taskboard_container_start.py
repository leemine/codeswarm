import subprocess
import os
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
base = root / ".taskboard-containers"
base.mkdir(exist_ok=True)
for name, port in [("alice", 19252), ("bob", 19262)]:
    d = base / name
    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "config/config.yaml").write_text(
        (root / ".taskboard-demo/config/config.yaml").read_text()
    )
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        "codex-taskboard-mvp-" + name,
        "--label",
        "codex.task=taskboard-mvp-demo",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--publish",
        f"127.0.0.1:{port}:18092",
        "--mount",
        f"type=bind,src={root}/jiuwenswarm,dst=/app/swarm/jiuwenswarm,readonly",
        "--mount",
        f"type=bind,src={d},dst=/taskboard-data",
        "--env",
        "JIUWENSWARM_DATA_DIR=/taskboard-data",
        "--env",
        "JIUWENSWARM_CONFIG_DIR=/taskboard-data/config",
        "--env",
        "AGENT_SERVER_HOST=0.0.0.0",
        "--env",
        "PYTHONPATH=/app/swarm",
        "--entrypoint",
        "python",
        "codeswarm-trial:20261010-web-pr45",
        "-m",
        "jiuwenswarm.server.app_agentserver",
        "--port",
        "18092",
    ]
    print(name, subprocess.check_output(cmd, text=True).strip())
