# Changelog

## 0.1.0 (2026-09-29)

First public version: a working exploration, not a finished product.

- Agent panel docked in FreeCAD 1.0 (Qt5/PySide2) and 1.1 (Qt6/PySide6), loaded from a
  folder link or `run_freecad.bat`; View menu entry and toolbar button.
- Engine process driving headless Claude Code through the Claude Agent SDK, isolated from
  the user's own Claude Code configuration, speaking a JSON-lines protocol (`docs/protocol.md`).
- Backends: local Ollama, Vast.ai GPU through a per-engine SSH tunnel, Anthropic API.
- Chat tabs with independent sessions, collapsible sidebar of past chats, history and
  resume, save chats as Markdown and load them into other chats as context.
- Streaming Markdown, live thinking and tool-call view, activity line with step and turn
  timers, `/` command palette, permissions selector, thinking-level selector.
- Attachments: images, PDFs (text plus page images for local models), Word, Excel,
  PowerPoint, text; CAD files as `@path`; viewport screenshots; drag and drop and paste.
- Skills, commands, subagents and MCP servers manager; FreeCAD application pack.
- Cloud cost guard: banner with price and GPU load, idle auto-destroy, instance watchdog,
  rent-from-FreeCAD dialog grouped by region, connect to an existing instance by ID.
