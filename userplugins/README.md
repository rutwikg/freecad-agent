# userplugins

Your own skills, commands, subagents and MCP servers. Everything here is a normal
Claude Code plugin folder (`<name>/.claude-plugin/plugin.json` plus `skills/`,
`commands/`, `agents/`, `.mcp.json`), so it also works in the Claude Code CLI.

The Agent panel creates `userplugins/user/` the first time you use **Skills & MCP**
(New skill…, Ask the agent to write one…, Add MCP server…). The bundled application
pack in `packs/` stays read-only; copy an item into your plugin to customise it.

The contents of this folder are not tracked by git.
