import asyncio
import json
from pathlib import Path
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.common.e2a.models import E2AEnvelope

OUT = Path("docs/taskboard/evidence/container-result.json")


async def main():
    clients = []
    seq = 0
    results = []

    async def rpc(c, method, params):
        nonlocal seq
        seq += 1
        return await c.send_request(
            E2AEnvelope(
                request_id=f"container-demo-{seq}",
                channel="web",
                user_id="untrusted-route-id",
                method=method,
                params=params,
            ),
            timeout=30,
        )

    try:
        for port in [19252, 19262]:
            c = WebSocketAgentServerClient()
            await c.connect(f"ws://127.0.0.1:{port}")
            clients.append(c)
        tasks = []
        for name, c in zip(["alice", "bob"], clients):
            response = await rpc(
                c,
                "taskboard.create",
                {
                    "title": f"{name} container task",
                    "client_create_id": name + "-create",
                },
            )
            assert response.ok, response
            tasks.append(response.payload["task"])
            results.append(name + " create through real container AgentServer E2A")
        for i, c in enumerate(clients):
            response = await rpc(c, "taskboard.list", {"status": "todo"})
            assert [x["task_id"] for x in response.payload["tasks"]] == [
                tasks[i]["task_id"]
            ]
            response = await rpc(
                c, "taskboard.get", {"task_id": tasks[1 - i]["task_id"]}
            )
            assert not response.ok and response.payload["code"] == "NOT_FOUND"
            results.append(["alice", "bob"][i] + " cannot read other instance task")
        OUT.write_text(
            json.dumps(
                {
                    "checks": results,
                    "tasks": tasks,
                    "scope": "two independent container AgentServers; not organization Gateway routing",
                },
                indent=2,
            )
        )
        print(json.dumps({"checks": results}))
    finally:
        for c in clients:
            await c.disconnect()


asyncio.run(main())
