"""
Friday MCP Server — Entry Point
Run with: python server.py
"""

from mcp.server.fastmcp import FastMCP
from friday.tools import register_all_tools
from friday.prompts import register_all_prompts
from friday.resources import register_all_resources
from friday.config import config

import os

# 安全預設：SSE 只綁 localhost。這個 server 暴露了可執行任意指令的工具，
# 一旦綁到 0.0.0.0 且無驗證，同網段任何人都能 RCE。要對外開放時，請明確
# 設定 MCP_HOST 並務必在前面加上驗證 / reverse proxy。
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# Create the MCP server instance
mcp = FastMCP(
    name=config.SERVER_NAME,
    host=MCP_HOST,
    port=MCP_PORT,
    instructions=(
        "You are Friday, a Tony Stark-style AI assistant. "
        "You have access to a set of tools to help the user. "
        "Be concise, accurate, and a little witty."
    ),
)

# Register tools, prompts, and resources
register_all_tools(mcp)
register_all_prompts(mcp)
register_all_resources(mcp)

def main():
    mcp.run(transport='sse')

if __name__ == "__main__":
    main()