# freecad-agent

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

An agent chat panel **inside FreeCAD**. Describe a part, drop in a PDF drawing or a
screenshot, and an AI agent builds and edits the model in the FreeCAD window you are
looking at.

It uses **Claude Code as a headless harness**: its agent loop, tools, permissions,
skills, subagents, slash commands and MCP support, driven through the Claude Agent SDK.
The model behind it is your choice: a **local model through Ollama** (tested with Qwen 3.8
27B on an RTX 3090), a **rented cloud GPU on Vast.ai** reached through an SSH tunnel, or the
**Anthropic API**. Everything FreeCAD-specific lives in a replaceable *application pack*,
so the same engine and panel can later serve other CAD/CAE programs.

```
 FreeCAD 1.0 / 1.1 ──────────────────────────────────────────┐
 │  Agent panel (Qt: tabs, sidebar, streaming, attachments)   │
 │  FreeCAD MCP RPC server on a private port                  │
 └──────────┬─────────────────────────────────▲───────────────┘
            │ JSON lines (docs/protocol.md)   │ XML-RPC
 Engine (own Python 3.12 venv)                │
   Claude Agent SDK → Claude Code CLI ── MCP: freecad-mcp
      │ plugins: packs/freecad-1.1 (skills, commands, subagents), userplugins/
      └ model: Ollama (local) │ Vast.ai GPU via SSH tunnel │ Anthropic API
```

---

## Why this exists

Pointing Claude Code at a local model is one environment variable. Making it *work* for
CAD, inside the CAD program, took solving problems that all fail **quietly**:

**1. Local models silently lose the start of the conversation.**
Claude Code assumes an unknown model has a 200k-token window and compacts at about 167k.
Ollama runs with 64k and truncates the prompt without saying so. The engine tells Claude
Code the real window (`CLAUDE_CODE_MAX_CONTEXT_TOKENS`), trims 13 built-in tools a CAD user
never needs (17.8k → 11.0k prompt tokens), and caps output so a confused model cannot
think until the limit.

**2. "Thinking effort" is quietly ignored, and long thoughts are thrown away.**
Claude Code asks every model for effort `high`; Qwen 3.8 only knows off/low/medium/xhigh,
so Ollama falls back to its default. A model planning a whole part in one 22,000-character
thought hit the output cap, the cut-off thought was discarded, and every retry started from
scratch. The panel reads the levels each model actually supports from Ollama, sends
`thinking: disabled` explicitly for *off*, and the FreeCAD pack tells the agent to build one
feature per step.

**3. Every open FreeCAD shares one MCP port.**
FreeCAD's XML-RPC server binds with `SO_REUSEADDR`, so on Windows every FreeCAD window
listens on 9875 and tool calls land in whichever one Windows picks, often one you cannot
see. The panel moves its own FreeCAD's RPC server to a private port and pins the MCP server
to it.

**4. Tools that appear mid-conversation force a full re-read.**
MCP servers connect a few seconds after a session starts. If the first prompt goes out
before that, the tool list changes and a local model must re-read the whole context
(55 s at 430 tokens/s). Sessions start when the panel opens and wait for MCP.

**5. Measuring context can load the model.**
Claude Code measures context with `count_tokens` requests to the backend. On a local
backend that could load 18 GB onto the GPU just because the panel opened; the engine only
measures after a turn.

**6. Shared tunnels cut each other off.**
Two FreeCAD windows sharing one SSH tunnel meant closing one aborted the other's cloud
request mid-answer. Each engine now owns its tunnel on its own port, with a keep-alive that
reopens it on the same port so the chat recovers by itself.

**7. Cloud GPUs keep billing when you forget them.**
The panel shows a cost banner (price, time, GPU load), destroys an idle GPU after a
countdown you can cancel, asks before FreeCAD closes with a GPU running, and installs a
watchdog on the instance that stops it even if your PC is off. `vastai destroy` asks its
own confirmation and silently aborts without `-y`; destroy is always verified.

---

## What you get

- **Agent panel** in FreeCAD 1.0 (Qt5/PySide2) and 1.1 (Qt6/PySide6): View → Agent chat.
- **Chat tabs** with independent sessions; a **sidebar** of past chats grouped by date;
  **History / Resume**; **Save** a chat as Markdown and **Load** saved chats into any other
  chat as context, across models and backends.
- **Streaming** Markdown replies, a live view of thinking and of tool calls as they are
  written, and an activity line with step and turn timers.
- **Attachments**: images, PDFs (text plus page images of drawings for local models),
  Word, Excel, PowerPoint, text and code; CAD files as `@path`; 📷 viewport screenshots;
  drag and drop and paste.
- **`/` command palette** for pack, user and useful built-in commands.
- **Permissions**: ask every time, auto-accept edits, trust CAD tools, plan only.
- **Thinking level** per model, from the model's own metadata.
- **Skills & MCP manager**: create, edit, copy and delete skills, commands, subagents and
  MCP servers, or ask the agent to write one.
- **Cloud GPUs**: rent from inside FreeCAD (offers grouped by region: RTX PRO 6000, A100,
  H100, H200, B200…), or connect to an instance you started on vast.ai by its ID.

---

## Requirements

- Windows 10/11 and **FreeCAD 1.0 or 1.1**.
- The **FreeCAD MCP addon** (`FreeCADMCP`, from [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp))
  installed in FreeCAD. The panel uses its RPC server.
- [**uv**](https://docs.astral.sh/uv/). It installs Python 3.12 and every dependency.
- At least one model backend (see below).

## Install

```powershell
git clone https://github.com/rutwikg/freecad-agent
cd freecad-agent
uv sync
```

`uv sync` builds `.venv`, including the Claude Agent SDK and the Claude Code CLI it
bundles, downloaded from PyPI on your machine.

Make FreeCAD load the panel, either permanently with a folder link:

```powershell
# FreeCAD 1.1
cmd /c mklink /J "%APPDATA%\FreeCAD\v1-1\Mod\FreeCADAgent" "%CD%\hosts\freecad\FreeCADAgent"
# FreeCAD 1.0
cmd /c mklink /J "%APPDATA%\FreeCAD\Mod\FreeCADAgent" "%CD%\hosts\freecad\FreeCADAgent"
```

or per launch with `run_freecad.bat` (finds FreeCAD 1.1 or 1.0; set `FREECAD_EXE` otherwise).

Open FreeCAD. After a second the **Agent** panel appears on the right, and its status line
should read `ready · MCP freecad ✓ 15 tools · FreeCAD 1.x · RPC port NNNNN`.
Try `/freecad:ping`, then "make a 20 mm cube".

## Choosing a model

| Backend | Setup | Notes |
|---|---|---|
| **Local (Ollama)** | Install [Ollama](https://ollama.com), set the user environment variable **`OLLAMA_CONTEXT_LENGTH=65536`**, restart Ollama, then `ollama pull qwen3.8:27b` | Needs a 24 GB GPU for Qwen 3.8 27B. Without the context variable Ollama's small default silently truncates the agent's prompts |
| **Vast.ai GPU** | `uv run vastai set api-key <key>`; generate an SSH key (`ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\openclaudecode_vast_ed25519 -N ""`) and add the `.pub` under Account → SSH Keys on vast.ai | Then **Rent…** in the panel. Qwen 3.8 27B q8 with a 128k context; first token about 1 s. Needs outbound SSH |
| **Anthropic API** | Set `ANTHROPIC_API_KEY` | Not yet exercised in this project |

Models per backend are listed in `agent_config.json`.

## Using the panel

- **Folder / Model / Allow / Thinking** at the top of each tab.
- **☰** opens the sidebar of past chats; **+** or **New chat** opens a tab with a fresh context.
- **History**, **Save**, **Load…**, **Skills & MCP** under the model selector.
- **📷 View** attaches the 3D view; drop files onto the message box or paste a screenshot.
- Type **`/`** for commands, including the panel's own `/new`, `/clear`, `/history`,
  `/save`, `/load`, `/skills`, `/view`, `/unload`.
- **Unload model** frees your local GPU; local models also unload after 2 idle minutes.

## Cloud GPUs on Vast.ai

Pick the Vast model in a tab, then either **Rent…** (offers grouped by region, filter by
GPU, confirm the price) or start an instance on vast.ai and enter its ID with **Connect**.
Connect checks SSH, starts Ollama with the right context length (alongside a template's
own Ollama if that one uses a smaller context), downloads the model with a progress bar,
and opens the tunnel. Ollama on the instance listens on 127.0.0.1 only; the model is never
exposed to the internet.

The command-line equivalent is `tools/cloud/vast.py` (`offers`, `create`, `status`,
`destroy`); `tools/cloud/bench.py` measures writing and reading speed of model variants.

## Configuration

`agent_config.json`:

| Key | Meaning |
|---|---|
| `backends.<name>.kind` | `ollama` or `anthropic` |
| `backends.<name>.models` | models offered in the picker (first = downloaded on Vast) |
| `context_window`, `max_output_tokens` | what Claude Code is told; keep in line with Ollama |
| `attachment_char_budget` | how much attached text one message may carry |
| `idle_unload_seconds`, `unload_on_close` | local GPU housekeeping |
| `idle_destroy_minutes`, `watchdog_minutes`, `home_region` | cloud cost guard and Rent dialog |
| `disallowed_tools` | built-in Claude Code tools hidden from the model |
| `default_thinking` | default thinking level per backend |
| `packs`, `userplugins`, `runtime` | where packs, user plugins and state live (all under this folder) |

All state (chats, settings, cloud record, logs) lives in `runtime/`; nothing is written to
`~/.claude`, and your own Claude Code configuration is never read (`CLAUDE_CONFIG_DIR` plus
`setting_sources=[]`).

## Extending

- **Application packs** (`packs/<app>-<version>/`) are ordinary Claude Code plugins:
  `skills/`, `commands/`, `agents/`, `.mcp.json`, plus `system_append.md` for standing
  instructions. The FreeCAD pack is the first; a second host needs a pack and a thin host
  adapter like `hosts/freecad/`.
- **User plugins** (`userplugins/`) hold what you create in the panel.
- The **engine protocol** (`docs/protocol.md`) is backend-neutral, so a different agent
  (for example an Agent SDK or LangGraph agent) can replace `engine/server.py` without
  touching the panel.

## Project layout

```
engine/        server.py (protocol, sessions, backends), attachments.py, cloud_vast.py
gui/           chat_widget.py (panel, tabs), sidebar, palette, history, manager, rent dialog, qt shim
hosts/freecad/ FreeCADAgent addon: dock, menu, private RPC port, viewport capture
packs/         freecad-1.1 application pack (skills, commands, subagent, MCP launcher)
docs/          protocol.md
tools/         smoke and regression tests, cloud tools, request capture, benchmarks
```

## Development and tests

The GUI can be exercised offscreen, without FreeCAD or a model:

```powershell
$env:QT_QPA_PLATFORM="offscreen"; uv run python tools/gui_offscreen_test.py
uv run python tools/attachments_test.py
uv run python tools/capture_backend_request.py session:effort=low   # shows exactly what Claude Code sends
```

`tools/smoke.py`, `tools/back_to_back_test.py` and `tools/cloud/tunnel_isolation_test.py`
drive real sessions (they load a model). Under FreeCAD 1.0 the panel can be checked with
FreeCAD's own interpreter: `"C:\Program Files\FreeCAD 1.0\bin\python.exe"`.

## Status

A working exploration, used daily by its author, not a finished product. Known gaps:
no diff view for file edits, basic plan-mode and question dialogs, no live MCP toggle,
the Anthropic backend is untested, and Windows is the only platform tried.
See `CHANGELOG.md`.

## Licence and third-party software

This repository is licensed under the **GNU Affero General Public License v3.0** (see
[LICENSE](LICENSE)).

It does **not** include or redistribute third-party programs. Each is installed on your
machine from its own source and used under its own terms:

- **Claude Code CLI**, bundled in Anthropic's `claude-agent-sdk` package and downloaded
  from PyPI by `uv sync`; use is subject to Anthropic's terms. Authenticate with your own
  API key.
- **Ollama** and the models you pull, **FreeCAD**, the **FreeCAD MCP** addon and server
  (`freecad-mcp`), and the Python packages listed in `pyproject.toml`.
