"""Agent engine: speaks the JSON-lines protocol in docs/protocol.md over stdio and
drives headless Claude Code sessions through the Claude Agent SDK.

Run:  uv run python -m engine.server
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import os
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))
RUNTIME = ROOT / CONFIG["runtime"]
CONFIG_DIR = RUNTIME / "claude-config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
# Set before importing the SDK so list_sessions() reads our isolated store too.
os.environ["CLAUDE_CONFIG_DIR"] = str(CONFIG_DIR)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    __version__ as SDK_VERSION,
    get_session_messages,
    list_sessions,
)

from engine.attachments import blocks_for as attachment_blocks  # noqa: E402

PROTOCOL = 1
ENGINE_VERSION = "0.1.0"

_out_lock = threading.Lock()


def emit(msg: dict[str, Any]) -> None:
    line = json.dumps(msg, ensure_ascii=False, default=str)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def plugin_dirs() -> list[dict[str, str]]:
    """Bundled packs, then every plugin folder under userplugins/."""
    dirs = [ROOT / p for p in CONFIG["packs"]]
    user_root = ROOT / CONFIG["userplugins"]
    if user_root.is_dir():
        dirs += sorted(p for p in user_root.iterdir() if (p / ".claude-plugin").is_dir())
    return [{"type": "local", "path": str(d)} for d in dirs]


def system_append() -> str:
    """Standing instructions from each pack's system_append.md, added to Claude Code's prompt."""
    parts = []
    for p in CONFIG["packs"]:
        f = ROOT / p / "system_append.md"
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def kind(backend: str) -> str:
    """How a backend is spoken to: "ollama" (local or tunnelled) or "anthropic"."""
    cfg = CONFIG["backends"][backend]
    return cfg.get("kind", backend)


def is_ollama(backend: str) -> bool:
    return kind(backend) == "ollama"


TUNNELS: dict[str, Any] = {}
_TUNNEL_LOCK = threading.Lock()


def ensure_tunnel(backend: str) -> None:
    """Open the SSH tunnel for a remote backend and wait until its Ollama answers.
    Serialised: model.info and session.start can ask at the same moment, and two ssh
    processes racing for the same local port leave a dead one tracked."""
    cfg = CONFIG["backends"][backend]
    if not cfg.get("tunnel"):
        return
    with _TUNNEL_LOCK:
        _ensure_tunnel(backend, cfg)


def _ensure_tunnel(backend: str, cfg: dict) -> None:
    from engine import cloud_vast
    # This engine's own tunnel. Borrowing another engine's (an earlier design) meant that
    # closing one FreeCAD cut off every other FreeCAD's chat mid-answer.
    t = TUNNELS.get(backend) or cloud_vast.Tunnel(cfg)
    TUNNELS[backend] = t
    if t.alive():
        try:
            _ollama_call("/api/version", backend=backend, timeout=5)
            return
        except Exception:
            t.close()  # stale tunnel: reopen
    log(f"[{backend}] {t.open()}")
    for _ in range(40):
        try:
            _ollama_call("/api/version", backend=backend, timeout=3)
            return
        except Exception:
            if not t.alive():
                err = t.proc.stderr.read().decode(errors="replace") if t.proc and t.proc.stderr else ""
                raise RuntimeError(f"SSH tunnel to {backend} failed: {err.strip()[:300]}")
            time.sleep(0.5)
    raise RuntimeError(f"{backend}: Ollama did not answer through the tunnel")


def thinking_levels(backend: str, model: str) -> dict[str, Any]:
    """Thinking levels the model supports, for the GUI's selector. Reads Ollama's model
    metadata (/api/show), which does not load the model."""
    if kind(backend) == "anthropic":
        return {"levels": ["off", "low", "medium", "high", "xhigh", "max"], "default": "high"}
    try:
        ensure_tunnel(backend)
        info = _ollama_call("/api/show", {"model": model}, backend=backend)
    except Exception as e:
        # Unknown, not "off": the GUI shows it as unavailable and asks again later
        # (Ollama may still be starting, e.g. right after an update).
        return {"levels": [], "default": None, "error": f"{type(e).__name__}: {e}"}
    t = info.get("thinking") or {}
    if "thinking" not in (info.get("capabilities") or []):
        return {"levels": ["off"], "default": "off"}
    levels = ["off" if v is False else v for v in t.get("values", []) if v is not True] or ["off", "on"]
    return {"levels": levels, "default": t.get("default") or levels[-1]}


def backend_env(backend: str, model: str) -> dict[str, str]:
    env = {
        "CLAUDE_CONFIG_DIR": str(CONFIG_DIR),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    cfg = CONFIG["backends"][backend]
    if is_ollama(backend):
        env |= {
            # Claude Code puts a per-request attribution hash in the prompt, which defeats
            # prefix caching on local servers (documented for vLLM; Ollama caches by prefix too).
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "ANTHROPIC_BASE_URL": backend_url(backend),
            "ANTHROPIC_AUTH_TOKEN": cfg["auth_token"],
            "ANTHROPIC_API_KEY": "",
            # Background tasks and subagents must not reach for a cloud model.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            # Local models can think in circles until the output cap; keep the cap small.
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(cfg.get("max_output_tokens", 8192)),
            # Unknown model names default to a 200k window, so compaction would come far
            # too late and Ollama would silently truncate. Match Ollama's real context.
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(cfg.get("context_window", 65536)),
        }
    elif kind(backend) == "anthropic" and cfg.get("auth") == "login":
        # Trial: the Claude account logged in inside runtime/claude-config (/login there).
        # An API key in the environment would take precedence, so blank it.
        env["ANTHROPIC_API_KEY"] = ""
    elif kind(backend) == "anthropic":
        env["ANTHROPIC_API_KEY"] = api_key(backend)
    return env


def api_key(backend: str) -> str:
    """The backend's API key: from its environment variable, else from its key file under
    runtime/ (git-ignored). The file is read at every chat start, so no restart is needed."""
    cfg = CONFIG["backends"][backend]
    key = os.environ.get(cfg.get("api_key_env", ""), "").strip()
    if not key and cfg.get("api_key_file"):
        try:
            key = (ROOT / CONFIG["runtime"] / cfg["api_key_file"]).read_text(encoding="utf-8-sig").strip()  # Notepad may add a BOM
        except OSError:
            pass
    if not key:
        where = f"the {cfg.get('api_key_env')} environment variable"
        if cfg.get("api_key_file"):
            where += f" or the file {CONFIG['runtime']}/{cfg['api_key_file']}"
        raise RuntimeError(f"no API key for '{backend}': put it in {where}")
    return key


def backend_url(backend: str) -> str:
    """Where to reach a backend: its configured URL, or for a tunnelled backend the local
    end of this engine's own tunnel."""
    cfg = CONFIG["backends"][backend]
    if cfg.get("tunnel"):
        from engine import cloud_vast
        t = TUNNELS.get(backend) or cloud_vast.Tunnel(cfg)
        TUNNELS[backend] = t
        return t.url
    return cfg["base_url"]


def _ollama_call(path: str, payload: dict | None = None, timeout: float = 10, backend: str = "ollama") -> dict:
    url = backend_url(backend).rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


async def ollama_loaded(backend: str = "ollama") -> list[dict[str, Any]]:
    try:
        ps = await asyncio.to_thread(_ollama_call, "/api/ps", None, 10, backend)
    except Exception:
        return []  # server not running, so nothing is in VRAM
    return [{"name": m["name"], "vram_bytes": m.get("size_vram", 0)} for m in ps.get("models", [])]


async def ollama_unload(only: list[str] | None = None, backend: str = "ollama") -> list[str]:
    """Evict models from VRAM: all loaded ones, or those in `only` that are loaded.
    Never sends keep_alive=0 for a model that is not loaded: Ollama would load it first."""
    loaded = [m["name"] for m in await ollama_loaded(backend)]
    names = [n for n in loaded if only is None or n in only]
    done = []
    for n in names:
        try:
            await asyncio.to_thread(_ollama_call, "/api/generate", {"model": n, "keep_alive": 0}, 10, backend)
            done.append(n)
        except Exception as e:
            log(f"unload {n} failed: {e}")
    if done:
        log(f"unloaded from VRAM: {done}")
    return done


SAVED_DIR = RUNTIME / "saved_sessions"


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._ -]+", "", text).strip().replace(" ", "-")
    return (s or "chat")[:60]


def save_session(backend_session_id: str, title: str, note: str = "", meta: dict | None = None) -> Path:
    """Write a chat as readable Markdown (saved_sessions/<title>.md) that can later be loaded
    into any new chat as context, on any model or backend. Thinking is left out; tool calls are
    kept with a one-line result so the next chat knows what was done."""
    meta = meta or {}
    items = history_items(backend_session_id)
    lines = [f"# {title}", "",
             f"- saved: {time.strftime('%Y-%m-%d %H:%M')}",
             f"- model: {meta.get('model', '?')} ({meta.get('backend', '?')})",
             f"- working folder: {meta.get('cwd', '?')}",
             f"- session id: {backend_session_id}"]
    if note:
        lines.append(f"- note: {note}")
    lines += ["", "## Conversation", ""]
    for it in items:
        kind = it["kind"]
        if kind == "user":
            lines += [f"**You:** {it['text'].strip()}", ""]
        elif kind == "assistant":
            lines += [f"**Agent:** {it['text'].strip()}", ""]
        elif kind == "tool":
            args = json.dumps(it.get("input"), ensure_ascii=False)
            lines.append(f"> tool `{it['name']}` {args[:400]}{'…' if len(args) > 400 else ''}")
        elif kind == "tool_result":
            first = (str(it.get("content") or "").strip().splitlines() or [""])[0][:200]
            lines += [f"> {'error' if it.get('is_error') else 'result'}: {first}", ""]
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    path = SAVED_DIR / f"{_slug(title)}.md"
    n = 2
    while path.exists():
        path = SAVED_DIR / f"{_slug(title)}-{n}.md"
        n += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def chat_title(summary: str | None) -> str:
    """A readable title: the first prompt without the attachment markers that precede it."""
    text = re.sub(r"<attachment\b.*?</attachment>", " ", summary or "", flags=re.S)
    text = re.sub(r"\[(?:image|attached [a-z]+ file)[^\]]*\]|\[[^\]]*page \d+ as an image:\]", " ", text)
    text = " ".join(text.split())
    return text or (summary or "(untitled)")


def trash_session(backend_session_id: str) -> list[str]:
    """Move a chat's transcript (and its subagent folder) to runtime/trash; recoverable."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,}", backend_session_id or ""):
        raise ValueError("not a session id")
    trash = RUNTIME / "trash" / time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for f in (CONFIG_DIR / "projects").glob(f"*/{backend_session_id}*"):
        trash.mkdir(parents=True, exist_ok=True)
        f.rename(trash / f.name)
        moved.append(str(trash / f.name))
    return moved


def saved_sessions() -> list[dict[str, Any]]:
    out = []
    for f in sorted(SAVED_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        head = f.read_text(encoding="utf-8")[:1500].splitlines()
        title = head[0].lstrip("# ").strip() if head else f.stem
        info = {ln[2:].split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in head if ln.startswith("- ") and ":" in ln}
        out.append({"path": str(f), "title": title, "saved": info.get("saved", ""), "model": info.get("model", ""),
                    "note": info.get("note", ""), "size_kb": round(f.stat().st_size / 1024, 1)})
    return out


def history_items(backend_session_id: str) -> list[dict[str, Any]]:
    """A past conversation flattened into the same shapes the GUI renders live."""
    items: list[dict[str, Any]] = []
    for m in get_session_messages(backend_session_id):
        content = (m.message or {}).get("content")
        if isinstance(content, str):
            if m.type == "user":
                items.append({"kind": "user", "text": content})
            continue
        for b in content or []:
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                items.append({"kind": "user" if m.type == "user" else "assistant", "text": b["text"]})
            elif t == "thinking" and b.get("thinking", "").strip():
                items.append({"kind": "thinking", "text": b["thinking"]})
            elif t == "tool_use":
                items.append({"kind": "tool", "name": b.get("name"), "input": b.get("input")})
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                items.append({"kind": "tool_result", "content": c, "is_error": bool(b.get("is_error"))})
    return items


class Session:
    def __init__(self, engine: Engine, sid: str, cmd: dict[str, Any]):
        self.engine = engine
        self.sid = sid
        self.backend = cmd.get("backend") or CONFIG["default"]["backend"]
        self.model = cmd.get("model") or CONFIG["default"]["model"]
        self.cwd = cmd.get("cwd") or str(Path.home())
        self.permission_mode = cmd.get("permission_mode") or "default"
        self.resume = cmd.get("resume")
        self.host_env = cmd.get("env") or {}  # from the host, e.g. FREECAD_RPC_PORT
        self.auto_allow: list[str] = cmd.get("auto_allow") or []  # tool-name prefixes the user chose to trust
        self.effort: str | None = cmd.get("effort")  # thinking level; "off" disables thinking; None = model default
        self.client: ClaudeSDKClient | None = None
        self.turn: asyncio.Task | None = None
        self.streamed_text = False
        self.streamed_thinking = False
        self.idle_unload: asyncio.Task | None = None

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            env=backend_env(self.backend, self.model) | self.host_env | (
                # With thinking disabled the CLI omits the field, and Ollama then thinks at the
                # model's default; send an explicit "disabled" instead.
                {"CLAUDE_CODE_EXTRA_BODY": json.dumps({"thinking": {"type": "disabled"}})}
                if self.effort == "off" else {}),
            setting_sources=[],
            strict_mcp_config=False,  # True would drop plugin .mcp.json servers
            plugins=plugin_dirs(),
            # The agent may write new skills, commands and agents into userplugins/.
            add_dirs=[str(ROOT / CONFIG["userplugins"])],
            skills="all",
            permission_mode=self.permission_mode,
            # Built-in tools a CAD user never needs cost ~7k prompt tokens per request.
            disallowed_tools=CONFIG.get("disallowed_tools", []),
            can_use_tool=self.can_use_tool,
            include_partial_messages=True,
            # Thinking level. Ollama honours output_config.effort for names the model lists
            # (qwen3.8: low/medium/xhigh) and falls back to the model default otherwise.
            effort=self.effort if self.effort not in (None, "", "off") else None,
            thinking={"type": "disabled"} if self.effort == "off" else None,
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_append()} if system_append() else None,
            resume=self.resume,
            stderr=lambda line: log(f"[cli {self.sid}] {line}"),
        )

    async def start(self) -> None:
        log(f"[{self.sid}] start backend={self.backend} model={self.model} effort={self.effort or 'model default'} "
            f"permissions={self.permission_mode} auto_allow={self.auto_allow} resume={self.resume}")
        await asyncio.to_thread(ensure_tunnel, self.backend)
        self.client = ClaudeSDKClient(options=self.options())
        await self.client.connect()
        # Wait for MCP servers before the first prompt: tools that appear mid-conversation
        # change the prompt prefix, and a local backend must then re-read the whole context.
        servers = await self.wait_for_mcp(CONFIG.get("mcp_wait_seconds", 30))
        emit({"type": "session.mcp", "session": self.sid, "servers": servers})
        try:
            emit({"type": "session.info", "session": self.sid, **await self.info()})
            # Measuring context sends count_tokens requests to the backend; on a local backend
            # that could load the model onto the GPU just because the chat panel opened.
            if not is_ollama(self.backend):
                emit({"type": "context", "session": self.sid, **await self.context()})
        except Exception as e:
            log(f"[{self.sid}] info/context failed: {e}")
        emit({"type": "session.started", "session": self.sid})

    async def info(self) -> dict[str, Any]:
        """Commands, skills and agents for the GUI's palette; needs no model call."""
        i = await self.client.get_server_info() or {}
        return {
            "commands": [{"name": c.get("name"), "description": c.get("description", ""),
                          "argument_hint": c.get("argumentHint", ""), "builtin": bool(c.get("builtin"))}
                         for c in i.get("commands", [])],
            "agents": [{"name": a.get("name"), "description": a.get("description", "")} for a in i.get("agents", [])],
            "output_styles": i.get("available_output_styles", []),
        }

    async def context(self) -> dict[str, Any]:
        u = await self.client.get_context_usage()
        return {"used": u.get("totalTokens"), "max": u.get("maxTokens"), "percentage": u.get("percentage"),
                "autocompact_at": u.get("autoCompactThreshold"),
                "categories": [{"name": c["name"], "tokens": c["tokens"]} for c in u.get("categories", [])
                               if c.get("kind") == "used"]}

    async def mcp_status(self) -> list[dict[str, Any]]:
        status = await self.client.get_mcp_status()
        return [{"name": s.get("name"), "status": s.get("status"), "error": s.get("error"),
                 "tools": len(s.get("tools") or [])} for s in status.get("mcpServers", [])]

    async def wait_for_mcp(self, timeout: float) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                servers = await self.mcp_status()
            except Exception as e:
                log(f"[{self.sid}] mcp status failed: {e}")
                return []
            if not any(s["status"] == "pending" for s in servers):
                return servers
            if asyncio.get_running_loop().time() > deadline:
                log(f"[{self.sid}] MCP still pending after {timeout}s: {servers}")
                return servers
            await asyncio.sleep(0.5)

    async def prompt(self, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        if self.turn and not self.turn.done():
            # The GUI re-enables Send on turn.end, a moment before the turn task has unwound:
            # queue behind it rather than reject the user's message.
            try:
                await asyncio.wait_for(asyncio.shield(self.turn), timeout=30)
            except asyncio.TimeoutError:
                emit({"type": "error", "session": self.sid, "message": "the previous turn is still running"})
                return
        self._cancel_idle_unload()
        if attachments:
            await self.client.query(self._with_attachments(text, attachments))
        else:
            await self.client.query(text)
        self.turn = asyncio.create_task(self._consume())

    async def _with_attachments(self, text: str, attachments: list[dict[str, Any]]):
        """One user message: attachment blocks (images, PDFs, documents) before the text."""
        budget = CONFIG["backends"][self.backend].get("attachment_char_budget", 60000)
        content = await asyncio.to_thread(
            attachment_blocks, [a["path"] for a in attachments], kind(self.backend), budget)
        content.append({"type": "text", "text": text})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    async def _consume(self) -> None:
        try:
            async for msg in self.client.receive_response():
                self._translate(msg)
        except Exception as e:  # keep the engine alive
            emit({"type": "error", "session": self.sid, "message": f"{type(e).__name__}: {e}"})
            emit({"type": "turn.end", "session": self.sid, "status": "error"})
        finally:
            self._schedule_idle_unload()
            # Measured outside the turn: it sends count_tokens requests to the backend, which
            # through a tunnel takes seconds, and the next prompt must not wait for it.
            asyncio.create_task(self._emit_context())

    async def _emit_context(self) -> None:
        try:
            emit({"type": "context", "session": self.sid, **await self.context()})
        except Exception as e:
            log(f"[{self.sid}] context measurement failed: {e}")

    # ----- VRAM housekeeping: a local model should not sit on the GPU when idle -----
    def _schedule_idle_unload(self) -> None:
        secs = CONFIG["backends"][self.backend].get("idle_unload_seconds", 0)
        if not is_ollama(self.backend) or not secs:
            return
        self._cancel_idle_unload()

        async def later():
            await asyncio.sleep(secs)
            unloaded = await self.engine.unload_if_unused(self.backend, self.model)
            if unloaded:
                emit({"type": "model.unloaded", "session": self.sid, "models": unloaded,
                      "reason": f"idle {secs}s"})

        self.idle_unload = asyncio.create_task(later())

    def _cancel_idle_unload(self) -> None:
        if self.idle_unload and not self.idle_unload.done():
            self.idle_unload.cancel()
        self.idle_unload = None

    def _translate(self, msg: Any) -> None:
        sid = self.sid
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            d = msg.data
            emit({
                "type": "session.catalog", "session": sid,
                "backend_session_id": d.get("session_id"),
                "catalog": {
                    "model": d.get("model"), "cwd": d.get("cwd"),
                    "tools": d.get("tools"), "skills": d.get("skills"),
                    "agents": d.get("agents"), "commands": d.get("slash_commands"),
                    "mcp_servers": d.get("mcp_servers"), "plugins": d.get("plugins"),
                    "permission_mode": d.get("permissionMode"),
                },
            })
        elif isinstance(msg, StreamEvent):
            ev = msg.event
            if ev.get("type") == "message_start":
                self.streamed_text = self.streamed_thinking = False
            elif ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                # The model is now writing a tool call (often a long script). Without this the
                # GUI sees nothing until the call is complete and looks stuck.
                self.writing_tool = ev["content_block"].get("name", "")
                self.writing_chars = 0
                self.writing_sent = 0.0
                emit({"type": "tool.writing", "session": sid, "name": self.writing_tool, "chars": 0,
                      "text": "", "parent": msg.parent_tool_use_id})
            elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "input_json_delta":
                part = ev["delta"].get("partial_json", "")
                self.writing_chars = getattr(self, "writing_chars", 0) + len(part)
                emit({"type": "tool.writing", "session": sid, "name": getattr(self, "writing_tool", ""),
                      "chars": self.writing_chars, "text": part, "parent": msg.parent_tool_use_id})
            elif ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.streamed_text = True
                    emit({"type": "text.delta", "session": sid, "text": delta.get("text", ""),
                          "parent": msg.parent_tool_use_id})
                elif delta.get("type") == "thinking_delta":
                    self.streamed_thinking = True
                    emit({"type": "thinking.delta", "session": sid, "text": delta.get("thinking", ""),
                          "parent": msg.parent_tool_use_id})
        elif isinstance(msg, AssistantMessage):
            parent = msg.parent_tool_use_id
            for b in msg.content:
                # Fall back to whole blocks when the backend did not stream.
                if isinstance(b, TextBlock) and not self.streamed_text:
                    emit({"type": "text.delta", "session": sid, "text": b.text, "parent": parent})
                elif isinstance(b, ThinkingBlock) and not self.streamed_thinking:
                    emit({"type": "thinking.delta", "session": sid, "text": b.thinking, "parent": parent})
                elif isinstance(b, ToolUseBlock):
                    emit({"type": "tool.start", "session": sid, "tool_use_id": b.id,
                          "name": b.name, "input": b.input, "parent": parent})
                    if b.name == "TodoWrite":
                        emit({"type": "todo.update", "session": sid, "todos": b.input.get("todos", [])})
            emit({"type": "message.end", "session": sid, "parent": parent})
            self.streamed_text = self.streamed_thinking = False
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    content = b.content
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    emit({"type": "tool.result", "session": sid, "tool_use_id": b.tool_use_id,
                          "content": content, "is_error": bool(b.is_error)})
        elif isinstance(msg, ResultMessage):
            status = "success" if msg.subtype == "success" else msg.subtype
            emit({"type": "turn.end", "session": sid, "status": status, "usage": msg.usage,
                  "cost_usd": msg.total_cost_usd, "duration_ms": msg.duration_ms,
                  "turns": msg.num_turns, "backend_session_id": msg.session_id})

    async def can_use_tool(self, tool: str, tool_input: dict[str, Any], ctx: ToolPermissionContext):
        rid = uuid.uuid4().hex
        if tool != "AskUserQuestion" and any(tool.startswith(p) for p in self.auto_allow):
            return PermissionResultAllow()
        if tool == "AskUserQuestion":
            emit({"type": "question.request", "session": self.sid, "request_id": rid,
                  "questions": tool_input.get("questions", [])})
            reply = await self.engine.wait_reply(rid)
            return PermissionResultAllow(updated_input={**tool_input, "answers": reply.get("answers", {})})
        emit({"type": "permission.request", "session": self.sid, "request_id": rid,
              "tool": tool, "input": tool_input, "tool_use_id": ctx.tool_use_id})
        reply = await self.engine.wait_reply(rid)
        if reply.get("decision") == "allow":
            return PermissionResultAllow(
                updated_input=reply.get("updated_input"),
                updated_permissions=ctx.suggestions if reply.get("remember") else None,
            )
        return PermissionResultDeny(message=reply.get("message") or "The user denied this action.")

    async def interrupt(self) -> None:
        if self.client:
            await self.client.interrupt()

    async def set(self, cmd: dict[str, Any]) -> None:
        if cmd.get("model"):
            self.model = cmd["model"]
            await self.client.set_model(self.model)
        if cmd.get("permission_mode"):
            self.permission_mode = cmd["permission_mode"]
            await self.client.set_permission_mode(self.permission_mode)
        if "auto_allow" in cmd:
            self.auto_allow = cmd["auto_allow"] or []

    async def close(self) -> None:
        self._cancel_idle_unload()
        if self.client:
            await self.client.disconnect()


class Engine:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.replies: dict[str, asyncio.Future] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop = asyncio.Event()
        self.inflight: set[asyncio.Task] = set()

    def busy_models(self) -> set[str]:
        return {s.model for s in self.sessions.values()
                if is_ollama(s.backend) and s.turn and not s.turn.done()}

    async def unload_if_unused(self, backend: str, model: str) -> list[str]:
        """Unload `model` unless another session is mid-turn with it."""
        if model in self.busy_models():
            return []
        return await ollama_unload([model], backend)

    # ----- cloud GPU (Vast.ai) -----
    async def cloud_attach(self, instance_id: int) -> None:
        from engine import cloud_vast
        cfg = CONFIG["backends"]["vast"]
        if "vast" in TUNNELS:  # the instance (and its remote port) may have changed
            TUNNELS.pop("vast").close()
        emit({"type": "cloud.progress", "text": f"checking instance {instance_id}…"})
        try:
            res = await asyncio.to_thread(cloud_vast.attach, cfg, instance_id)
        except Exception as e:
            res = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        emit({"type": "cloud.attached", **res})
        if res.get("ok") and res.get("model_state") != "present":
            asyncio.create_task(self._watch_pull())

    async def _watch_pull(self) -> None:
        from engine import cloud_vast
        cfg = CONFIG["backends"]["vast"]
        for _ in range(360):  # up to 30 minutes
            try:
                text = await asyncio.to_thread(cloud_vast.pull_progress, cfg)
            except Exception as e:
                text = f"(progress unavailable: {e})"
            if text == "done":
                emit({"type": "cloud.progress", "text": "model ready", "ready": True})
                return
            pct = re.match(r"(\d+)%", text or "")
            emit({"type": "cloud.progress", "text": f"downloading model: {text}" if text else "downloading model…",
                  "percent": int(pct.group(1)) if pct else None})
            await asyncio.sleep(5)

    async def cloud_create(self, offer_id: int) -> None:
        """Rent an offer (the GUI has already confirmed the price with the user), wait for it
        to boot, then attach. COSTS MONEY until destroyed."""
        from engine import cloud_vast
        cfg = CONFIG["backends"]["vast"]
        try:
            emit({"type": "cloud.progress", "text": f"renting offer {offer_id}…"})
            res = await asyncio.to_thread(cloud_vast.create_instance, offer_id, cfg["models"][0], cfg["context_window"])
            iid = res["instance_id"]
            for i in range(90):  # up to 15 minutes to boot
                inst = await asyncio.to_thread(cloud_vast.instance, iid)
                state = (inst or {}).get("actual_status") or "starting"
                emit({"type": "cloud.progress", "text": f"instance {iid}: {state} ({i * 10}s)", "instance_id": iid})
                if state == "running" and cloud_vast.ssh_endpoint(inst):
                    break
                await asyncio.sleep(10)
            await asyncio.sleep(15)  # let the start-up script repair SSH key permissions
            await self.cloud_attach(iid)
        except Exception as e:
            emit({"type": "cloud.attached", "ok": False, "message": f"rent failed: {e}"})

    async def cloud_resume(self, instance_id: int) -> None:
        """Start a stopped instance again (the model is still on its disk), then attach."""
        from engine import cloud_vast
        try:
            emit({"type": "cloud.progress", "text": f"starting instance {instance_id}…"})
            await asyncio.to_thread(cloud_vast.start, instance_id)
            for i in range(60):
                inst = await asyncio.to_thread(cloud_vast.instance, instance_id)
                state = (inst or {}).get("actual_status") or "starting"
                emit({"type": "cloud.progress", "text": f"instance {instance_id}: {state} ({i * 10}s)"})
                if state == "running" and cloud_vast.ssh_endpoint(inst):
                    break
                await asyncio.sleep(10)
            await asyncio.sleep(15)
            await self.cloud_attach(instance_id)
        except Exception as e:
            emit({"type": "cloud.attached", "ok": False, "message": f"start failed: {e}"})

    async def wait_reply(self, rid: str) -> dict[str, Any]:
        fut = self.loop.create_future()
        self.replies[rid] = fut
        try:
            return await fut
        finally:
            self.replies.pop(rid, None)

    async def handle(self, cmd: dict[str, Any]) -> None:
        t = cmd.get("type")
        sid = cmd.get("session")
        try:
            if t == "hello":
                emit({"type": "ready", "protocol": PROTOCOL, "engine": ENGINE_VERSION,
                      "backend_versions": {"claude_agent_sdk": SDK_VERSION}})
            elif t == "session.start":
                s = Session(self, sid, cmd)
                self.sessions[sid] = s
                await s.start()
            elif t == "prompt":
                await self.sessions[sid].prompt(cmd["text"], cmd.get("attachments"))
            elif t == "interrupt":
                await self.sessions[sid].interrupt()
            elif t in ("permission.reply", "question.reply"):
                fut = self.replies.get(cmd.get("request_id"))
                if fut and not fut.done():
                    fut.set_result(cmd)
            elif t == "session.set":
                await self.sessions[sid].set(cmd)
            elif t == "session.close":
                s = self.sessions.pop(sid, None)
                if s:
                    await s.close()
                    if is_ollama(s.backend) and CONFIG["backends"][s.backend].get("unload_on_close", True):
                        if not any(o.model == s.model for o in self.sessions.values()):
                            unloaded = await ollama_unload([s.model], s.backend)
                            if unloaded:
                                emit({"type": "model.unloaded", "models": unloaded, "reason": "session closed"})
            elif t == "session.save":
                path = await asyncio.to_thread(save_session, cmd["backend_session_id"], cmd.get("title") or "chat",
                                               cmd.get("note", ""), cmd.get("meta"))
                emit({"type": "session.saved", "session": sid, "path": str(path)})
            elif t == "saved.list":
                emit({"type": "saved.list", "session": sid, "for": cmd.get("for"),
                      "items": await asyncio.to_thread(saved_sessions)})
            elif t == "session.delete":
                moved = await asyncio.to_thread(trash_session, cmd["backend_session_id"])
                emit({"type": "session.deleted", "for": cmd.get("for"),
                      "backend_session_id": cmd["backend_session_id"], "moved": moved})
            elif t == "session.history":
                emit({"type": "history", "session": sid, "items": history_items(cmd["backend_session_id"])})
            elif t == "context.get":
                emit({"type": "context", "session": sid, **await self.sessions[sid].context()})
            elif t == "mcp.status":
                emit({"type": "session.mcp", "session": sid, "servers": await self.sessions[sid].mcp_status()})
            elif t == "cloud.attach":
                await self.cloud_attach(int(cmd["instance_id"]))
            elif t == "cloud.detach":
                # Close the tunnel and mark the instance detached. It stays recorded, so the
                # cost banner keeps showing a GPU that is still billing.
                from engine import cloud_vast
                if "vast" in TUNNELS:
                    TUNNELS.pop("vast").close()
                st = cloud_vast.load_state()
                if st:
                    cloud_vast.save_state(st | {"attached": False})
                emit({"type": "cloud.detached", "instance_id": st.get("instance_id")})
            elif t == "cloud.offers":
                from engine import cloud_vast
                offers = await asyncio.to_thread(cloud_vast.search_offers, int(cmd.get("min_vram") or 40))
                emit({"type": "cloud.offers", "offers": offers})
            elif t == "cloud.create":
                asyncio.create_task(self.cloud_create(int(cmd["offer_id"])))
            elif t == "cloud.start":
                asyncio.create_task(self.cloud_resume(int(cmd["instance_id"])))
            elif t == "cloud.status":
                from engine import cloud_vast
                st = await asyncio.to_thread(cloud_vast.status, CONFIG["backends"]["vast"], True)
                emit({"type": "cloud.status", "backend": "vast", **st})
            elif t == "cloud.destroy":
                from engine import cloud_vast
                if "vast" in TUNNELS:
                    TUNNELS.pop("vast").close()
                await asyncio.to_thread(cloud_vast.destroy, cmd.get("instance_id"))
                emit({"type": "cloud.status", "backend": "vast", "running": False, "destroyed": True})
            elif t == "model.info":
                info = await asyncio.to_thread(thinking_levels, cmd["backend"], cmd["model"])
                emit({"type": "model.info", "backend": cmd["backend"], "model": cmd["model"], **info})
            elif t == "model.status":
                emit({"type": "model.status", "loaded": await ollama_loaded()})
            elif t == "model.unload":
                # Explicit user request: evict everything, even mid-turn (Ollama reloads on demand).
                unloaded = await ollama_unload(cmd.get("models"), cmd.get("backend") or "ollama")
                emit({"type": "model.unloaded", "models": unloaded, "reason": "user request"})
            elif t == "sessions.list":
                items = await asyncio.to_thread(list_sessions, cmd.get("cwd"), int(cmd.get("limit") or 50))
                emit({"type": "sessions", "for": cmd.get("for"), "items": [
                    {"backend_session_id": i.session_id, "summary": chat_title(i.summary),
                     "last_modified": i.last_modified, "cwd": i.cwd} for i in items]})
            elif t == "config.get":
                emit({"type": "config", "default": CONFIG["default"],
                      "backends": {k: {"models": v["models"]} for k, v in CONFIG["backends"].items()},
                      "plugins": plugin_dirs()})
            elif t == "shutdown":
                self.stop.set()
            else:
                emit({"type": "error", "session": sid, "message": f"unknown message type: {t}"})
        except Exception as e:
            emit({"type": "error", "session": sid, "message": f"{t}: {type(e).__name__}: {e}"})

    def _stdin_reader(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as e:
                emit({"type": "error", "message": f"bad json: {e}"})
                continue
            self.loop.call_soon_threadsafe(self._dispatch, cmd)
        self.loop.call_soon_threadsafe(self.stop.set)  # GUI closed our stdin

    def _dispatch(self, cmd: dict[str, Any]) -> None:
        task = self.loop.create_task(self.handle(cmd))
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)

    async def _keep_tunnels(self) -> None:
        """Reopen a dead tunnel on the same local port, so the chat's base URL stays valid and
        the CLI's automatic retries succeed (after a network blip or an SSH drop)."""
        while not self.stop.is_set():
            await asyncio.sleep(10)
            for backend, t in list(TUNNELS.items()):
                if t.proc is not None and not t.alive():  # was open, has died
                    log(f"[{backend}] tunnel died; reopening on port {t.local_port}")
                    try:
                        await asyncio.to_thread(ensure_tunnel, backend)
                        emit({"type": "cloud.progress", "text": "reconnected to the cloud GPU"})
                    except Exception as e:
                        log(f"[{backend}] reopen failed: {e}")

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._stdin_reader, daemon=True).start()
        asyncio.create_task(self._keep_tunnels())
        await self.stop.wait()
        # Free the GPU first: the host may kill us within seconds, and closing a
        # session that is mid-turn can take longer than that.
        used = {(s.backend, s.model) for s in self.sessions.values()
                if is_ollama(s.backend) and CONFIG["backends"][s.backend].get("unload_on_close", True)}
        for backend in {b for b, _ in used}:
            await ollama_unload([m for b, m in used if b == backend], backend)
        # Let quick in-flight commands (status, unload, replies) finish before exiting.
        pending = [t for t in self.inflight if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=2)
        for s in list(self.sessions.values()):
            try:
                await asyncio.wait_for(s.close(), timeout=2)
            except Exception:
                pass
        for t in TUNNELS.values():
            t.close()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(Engine().run())


if __name__ == "__main__":
    main()
