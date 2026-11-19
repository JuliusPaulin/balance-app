"""The MCP server as a separate process, speaking stdio.

The child uses the suite's throwaway database. It does not open a port.
"""

import asyncio
import sys
from pathlib import Path

import config
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def _round_trip():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "balance_mcp"],
        cwd=ROOT,
        env={"SQLITE_PATH": config.SQLITE_PATH},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert "commit_bank_import" in names
            assert "undo_change" in names
            assert "preview_bank_import" in names
            result = await session.call_tool("list_categories", {})
            assert result.is_error is False
            text = result.content[0].text
            assert "Groceries" in text
            refused = await session.call_tool(
                "commit_bank_import",
                {"path": "/no/such/file.csv", "expected_new": 1},
            )
            assert refused.is_error is True
            assert "not a file" in refused.content[0].text


def test_stdio_server_reads_categories_and_refuses_a_missing_file():
    asyncio.run(_round_trip())
