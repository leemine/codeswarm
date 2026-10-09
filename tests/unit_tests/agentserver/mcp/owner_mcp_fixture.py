"""Real local MCP transport fixture; no external services or real credentials."""
import asyncio

from mcp.server.fastmcp import FastMCP

server = FastMCP('owner-mcp-test')


@server.tool()
def add(a: int, b: int) -> int:
    return a + b


@server.tool()
async def delayed() -> str:
    await asyncio.sleep(0.3)
    return 'late-private-result'


if __name__ == '__main__':
    server.run(transport='stdio')
