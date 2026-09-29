"""Spike 0a/0b: drive the bundled Claude Code CLI headless, isolated from ~/.claude,
with the FreeCAD pack and user plugins loaded, against a local Ollama model.

Checks:
  1. The init message lists our plugins, skills, agents, commands and MCP server,
     and nothing from the user's global ~/.claude (e.g. catia, abaqus-mcp).
  2. The model can invoke a pack skill and a user skill (check phrases).
  3. A plugin slash command works when sent as a prompt.

Usage:  uv run python tools/spikes/spike_0a.py [model]  (historical: the first spike)
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemma4:26b"

CONFIG_DIR = ROOT / "runtime" / "claude-config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

ENV = {
    # Isolation: sessions, settings and credentials live under the package, not ~/.claude
    "CLAUDE_CONFIG_DIR": str(CONFIG_DIR),
    # Local backend: Ollama speaks the Anthropic Messages API natively
    "ANTHROPIC_BASE_URL": "http://localhost:11434",
    "ANTHROPIC_AUTH_TOKEN": "ollama",
    "ANTHROPIC_API_KEY": "",
    # Every model alias the harness might reach for maps to the local model
    "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
    "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}


def options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        cwd=str(ROOT / "sandbox" / "workdir"),
        env=ENV,
        setting_sources=[],  # ignore ~/.claude and project settings
        strict_mcp_config=False,  # True would also drop plugin .mcp.json servers; isolation comes from CLAUDE_CONFIG_DIR + setting_sources
        plugins=[
            {"type": "local", "path": str(ROOT / "packs" / "freecad-1.1")},
            {"type": "local", "path": str(ROOT / "userplugins" / "user")},
        ],
        skills="all",
        permission_mode="bypassPermissions",  # spike only; the GUI will use can_use_tool
        max_turns=8,
    )


def show(msg) -> None:
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        d = msg.data
        print("== INIT ==")
        for key in ("model", "cwd", "plugins", "skills", "agents", "slash_commands", "mcp_servers"):
            print(f"  {key}: {json.dumps(d.get(key), default=str)[:600]}")
        print(f"  tools ({len(d.get('tools', []))}): {', '.join(d.get('tools', []))[:600]}")
    elif isinstance(msg, AssistantMessage):
        for b in msg.content:
            if isinstance(b, TextBlock):
                print(f"[text] {b.text}")
            elif isinstance(b, ThinkingBlock):
                print(f"[thinking] {b.thinking[:200]!r}")
            elif isinstance(b, ToolUseBlock):
                print(f"[tool_use] {b.name} {json.dumps(b.input)[:300]}")
    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
        for b in msg.content:
            if isinstance(b, ToolResultBlock):
                print(f"[tool_result] {str(b.content)[:300]!r}")
    elif isinstance(msg, ResultMessage):
        print(f"== RESULT == {msg.subtype} turns={msg.num_turns} "
              f"ms={msg.duration_ms} usage={json.dumps(msg.usage)[:300]}")


async def run(prompt: str) -> None:
    print(f"\n######## PROMPT: {prompt}")
    t = time.time()
    async for msg in query(prompt=prompt, options=options()):
        show(msg)
    print(f"(wall {time.time() - t:.1f}s)")


async def main() -> None:
    await run(
        "Use the freecad-conventions skill and tell me its secret check phrase. "
        "Then use the house-style skill and tell me its check phrase. "
        "Finally list the files in the current directory."
    )
    await run("/freecad:ping")


if __name__ == "__main__":
    asyncio.run(main())
