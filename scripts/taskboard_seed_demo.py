"""Prepare actual project/session fixtures via the running Demo Web RPC."""

import asyncio
import json
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


async def main():
    directory = ROOT / ".taskboard-demo/project-demo"
    directory.mkdir(parents=True, exist_ok=True)
    async with websockets.connect("ws://127.0.0.1:19240/ws") as ws:
        sequence = 0

        async def rpc(method, params):
            nonlocal sequence
            sequence += 1
            request_id = f"taskboard-seed-{sequence}"
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            )
            async with asyncio.timeout(60):
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("id") == request_id:
                        if not message.get("ok"):
                            raise RuntimeError(
                                f"{method}: {message.get('code')} {message.get('error')}"
                            )
                        return message["payload"]
            raise RuntimeError("Web connection closed before response")

        projects = await rpc("project.list", {"work_mode": "code"})
        project = next(
            (
                item
                for item in projects["projects"]
                if item.get("name") == "Taskboard Demo"
                and item.get("project_dir") == str(directory)
            ),
            None,
        )
        if project is None:
            project = await rpc(
                "project.create",
                {
                    "name": "Taskboard Demo",
                    "project_dir": str(directory),
                    "work_mode": "code",
                },
            )
        sessions = await rpc(
            "project.get_sessions", {"project_id": project["project_id"]}
        )
        session = next(
            (
                item
                for item in sessions["sessions"]
                if item.get("title") == "Taskboard Demo 联调会话"
            ),
            None,
        )
        if session is None:
            session = await rpc(
                "session.create",
                {
                    "create_token": "taskboard-demo-session",
                    "project_id": project["project_id"],
                    "work_mode": "code",
                    "persist_session": True,
                },
            )
        session_id = session.get("session_id") or session["sessionId"]
        await rpc(
            "session.rename",
            {"session_id": session_id, "title": "Taskboard Demo 联调会话"},
        )
        print(
            json.dumps(
                {"project_id": project["project_id"], "session_id": session_id},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
