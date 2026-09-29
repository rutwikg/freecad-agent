# GUI ↔ engine protocol (v1)

The GUI (chat dock inside the host application) starts the engine as a child
process and talks to it over **stdin/stdout, one JSON object per line** (UTF-8).
stderr is free-form logging. The protocol is deliberately backend-neutral: the
engine today wraps the Claude Agent SDK, but an Agent SDK or LangGraph agent can
sit behind the same messages later without touching the GUI.

Every message has a `type`. Messages about a conversation carry `session`, a
GUI-chosen id (not the backend's id). Requests that expect a reply carry
`request_id`.

## GUI → engine (commands)

| type | fields | meaning |
|---|---|---|
| `hello` | `protocol` | handshake; engine answers `ready` |
| `session.start` | `session`, `cwd`, `backend`, `model`, `permission_mode`, `auto_allow?`, `env?`, `resume?` | open a conversation. `auto_allow`: tool-name prefixes the user chose to trust (approved without asking). `env`: host-provided variables for tools and MCP servers (e.g. `FREECAD_RPC_PORT`). `resume`: a backend session id from `sessions.list` |
| `prompt` | `session`, `text`, `attachments?` | user turn; `text` may be a slash command. Attachments: `{kind: "file", path}`; the engine converts each per backend (images, PDFs, Office, text; CAD files become a pointer) within `attachment_char_budget` |
| `interrupt` | `session` | stop the current turn |
| `permission.reply` | `request_id`, `decision` (`allow`/`deny`), `remember?`, `message?`, `updated_input?` | answer to `permission.request` |
| `question.reply` | `request_id`, `answers` | answer to `question.request` |
| `session.set` | `session`, `model?`, `permission_mode?`, `auto_allow?` | change settings mid-conversation |
| `session.close` | `session` | end the conversation and free the backend |
| `mcp.status` | `session` | live MCP server status; answered with `session.mcp` |
| `sessions.list` | `cwd?` | past sessions for resume |
| `session.history` | `session`, `backend_session_id` | past messages of a session, answered with `history` |
| `context.get` | `session` | context-window usage, answered with `context` |
| `config.get` | | backends, models, paths |
| `model.status` | | which local models are in VRAM |
| `model.unload` | `models?` | evict local models from VRAM now (all when omitted) |
| `cloud.attach` | `instance_id` | connect to a running Vast instance: SSH check, Ollama with our settings, model pull |
| `cloud.offers` | `min_vram?` | current Vast offers |
| `cloud.create` | `offer_id` | rent an offer (GUI confirms price first), boot, then attach |
| `cloud.status` / `cloud.destroy` | `instance_id?` | cost guard |
| `shutdown` | | exit cleanly |

## Engine → GUI (events)

| type | fields | meaning |
|---|---|---|
| `ready` | `protocol`, `engine`, `backend_versions` | handshake reply |
| `session.mcp` | `session`, `servers` (`[{name, status, error, tools}]`) | MCP servers settled (sent before `session.started`), or reply to `mcp.status` |
| `session.started` | `session` | backend connected and MCP servers settled; ready for `prompt` |
| `session.catalog` | `session`, `backend_session_id`, `catalog` | sent at the start of each turn; `catalog` = `{model, cwd, tools, skills, agents, commands, mcp_servers, plugins}` |
| `text.delta` | `session`, `text` | streamed assistant text |
| `thinking.delta` | `session`, `text` | streamed reasoning (GUI may collapse) |
| `message.end` | `session` | end of one assistant message |
| `tool.start` | `session`, `tool_use_id`, `name`, `input`, `parent?` | a tool call began; `parent` is set inside subagents |
| `tool.result` | `session`, `tool_use_id`, `content`, `is_error` | tool call finished |
| `todo.update` | `session`, `todos` | task list changed |
| `permission.request` | `session`, `request_id`, `tool`, `input` | GUI must answer with `permission.reply` |
| `question.request` | `session`, `request_id`, `questions` | model asks the user; answer with `question.reply` |
| `turn.end` | `session`, `status`, `usage`, `cost_usd?`, `duration_ms`, `turns` | turn finished (`success`, `interrupted`, `error`) |
| `sessions` | `items` | reply to `sessions.list` |
| `history` | `session`, `items` (`[{kind: user/assistant/thinking/tool/tool_result, ...}]`) | reply to `session.history` |
| `session.info` | `session`, `commands` (`[{name, description, argument_hint, builtin}]`), `agents`, `output_styles` | sent before `session.started`; feeds the command palette |
| `context` | `session`, `used`, `max`, `percentage`, `autocompact_at`, `categories` | context-window usage; sent at session start and after every turn |
| `config` | `...` | reply to `config.get` |
| `model.status` | `loaded` (`[{name, vram_bytes}]`) | reply to `model.status` |
| `model.unloaded` | `models`, `reason`, `session?` | models left VRAM (user request, idle timeout, session closed) |
| `cloud.progress` | `text`, `ready?` | rent/attach/download progress |
| `cloud.attached` | `ok`, `message`, `port?`, `model_state?` | result of `cloud.attach` |
| `cloud.offers` | `offers` | reply to `cloud.offers` |
| `cloud.status` | `running`, `instance_id`, `gpu`, `price_h`, `hours`, `cost_so_far` | cost guard |
| `error` | `session?`, `message` | something failed; the engine stays up |

## Rules

- The engine never blocks the GUI: every command is acknowledged by events, and
  long work streams.
- One engine process serves many sessions.
- Unknown message types are answered with `error` and otherwise ignored, so
  GUI and engine versions can drift a little.
