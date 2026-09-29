"""Host-independent chat widget. Starts the engine as a child process and speaks
docs/protocol.md with it. Embed ChatWidget in any Qt host (FreeCAD dock), or run
this file directly for a standalone window:

    uv run python -m gui.chat_widget
"""

from __future__ import annotations

import html
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

from .history import HistoryDialog, LoadContextDialog
from .manager import ManagerDialog
from .markdown import md_to_html
from .palette import CommandPalette
from .qt import QShortcut, QtCore, QtGui, QtWidgets
from .rent_dialog import RentDialog
from .sidebar import ChatSidebar

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / CONFIG["runtime"] / "gui_state.json"
VAST_STATE = ROOT / CONFIG["runtime"] / "cloud" / "vast.json"


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(**changes) -> None:
    try:
        STATE_FILE.write_text(json.dumps(_read_state() | changes), encoding="utf-8")
    except OSError:
        pass


def _vast_state() -> dict:
    try:
        return json.loads(VAST_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _recorded_instance():
    """The rented instance we know about (connected or not): the cost guard tracks it."""
    return _vast_state().get("instance_id")


def _connected_instance():
    """The instance chats may use: recorded and not disconnected by the user."""
    st = _vast_state()
    return st.get("instance_id") if st.get("attached", True) else None
ATTACH_DIR = ROOT / CONFIG["runtime"] / "attachments"

# Commands the GUI handles itself, shown in the palette next to the agent's.
LOCAL_COMMANDS = [
    {"name": "new", "description": "Start a new chat in a new tab"},
    {"name": "clear", "description": "Clear this chat and start over in this tab"},
    {"name": "history", "description": "Resume an earlier chat"},
    {"name": "skills", "description": "Manage skills, commands, subagents and MCP servers"},
    {"name": "view", "description": "Attach a screenshot of the 3D view to the next message"},
    {"name": "save", "description": "Save this chat (as Markdown) to load into other chats later"},
    {"name": "load", "description": "Attach saved chats to the next message as context"},
    {"name": "unload", "description": "Free the GPU: unload the local model now"},
]


def engine_python() -> str:
    exe = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(exe)


class EngineClient(QtCore.QObject):
    # Not named "event": that would shadow QObject.event(), which Qt calls to deliver every event.
    message = QtCore.Signal(dict)
    stopped = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QtCore.QProcess(self)
        self.proc.setWorkingDirectory(str(ROOT))
        env = QtCore.QProcessEnvironment.systemEnvironment()
        # Hosts such as FreeCAD point these at their own Python; the engine has its own.
        for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.remove(var)
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(lambda code, _status: self.stopped.emit(f"engine exited ({code})"))
        self._buf = b""

    def start(self) -> None:
        self.proc.start(engine_python(), ["-m", "engine.server"])
        self.send({"type": "hello", "protocol": 1})

    def send(self, msg: dict) -> None:
        self.proc.write((json.dumps(msg) + "\n").encode("utf-8"))

    def _read(self) -> None:
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if line.strip():
                try:
                    self.message.emit(json.loads(line.decode("utf-8")))
                except json.JSONDecodeError:
                    pass

    def _read_err(self) -> None:
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        log = ROOT / CONFIG["runtime"] / "engine_stderr.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(data)

    def shutdown(self) -> None:
        try:
            self.proc.finished.disconnect()  # the widget may already be gone when the engine exits
        except (RuntimeError, TypeError):
            pass
        if self.proc.state() != QtCore.QProcess.NotRunning:
            self.send({"type": "shutdown"})
            if not self.proc.waitForFinished(6000):
                self.proc.kill()


VERBS = [
    "Contemplating", "Pondering", "Sketching", "Extruding thoughts", "Filleting ideas",
    "Constraining", "Chamfering", "Meshing", "Tessellating", "Revolving", "Lofting",
    "Measuring twice", "Consulting the datum", "Jibber-jabbering", "Mulling", "Noodling",
]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def pretty_tool(name: str) -> str:
    """mcp__plugin_freecad_freecad__create_object -> freecad · create_object"""
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].split("_")[-1] if len(parts) > 2 else parts[1]
        return f"{server} · {parts[-1]}"
    return name


class ActivityBar(QtWidgets.QWidget):
    """Shows that the agent is alive: spinner, whimsical verb, phase and elapsed time,
    plus a collapsible live view of the model's thinking."""

    def __init__(self, parent=None, show_thinking: bool = False):
        super().__init__(parent)
        self.label = QtWidgets.QLabel()
        self.label.setStyleSheet("color: #c60;")
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setChecked(show_thinking)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolTip("Show the model's thinking live")
        self.toggle.toggled.connect(self._toggled)
        self.pane = QtWidgets.QPlainTextEdit()
        self.pane.setReadOnly(True)
        self.pane.setMaximumHeight(140)
        self.pane.setStyleSheet("color: gray; font-style: italic;")
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label, 1)
        row.addWidget(self.toggle)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.pane)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.frame = 0
        self.t0 = 0.0
        self.first_thought = True
        self.verb = VERBS[0]
        self.phase = ""
        self._toggled(show_thinking)
        self.hide()

    def start(self, phase: str) -> None:
        self.t0 = self.step_t0 = time.monotonic()
        self.verb = random.choice(VERBS)
        self.first_thought = True
        self.phase = phase
        self.pane.clear()
        self.show()
        self.timer.start(100)
        self._tick()

    def set_phase(self, phase: str) -> None:
        # A new kind of activity starts a new step; updates to the same one (e.g. a growing
        # character count) do not reset the step timer.
        if phase.split("…")[0] != self.phase.split("…")[0]:
            self.step_t0 = time.monotonic()
        self.phase = phase

    def new_thought(self) -> None:
        """A fresh block of thinking began: give it its own verb.
        The turn's first thought keeps the verb chosen in start()."""
        if self.first_thought:
            self.first_thought = False
            return
        choices = [v for v in VERBS if v != self.verb]
        self.verb = random.choice(choices)

    def stop(self) -> None:
        self.timer.stop()
        self.hide()

    def add_thinking(self, text: str) -> None:
        cur = self.pane.textCursor()
        cur.movePosition(QtGui.QTextCursor.End)
        cur.insertText(text)
        self.pane.setTextCursor(cur)
        self.pane.ensureCursorVisible()

    def _toggled(self, on: bool) -> None:
        self.toggle.setText("hide thinking ▾" if on else "show thinking ▸")
        self.pane.setVisible(on)

    def _tick(self) -> None:
        now = time.monotonic()
        self.frame = (self.frame + 1) % len(SPINNER)
        waiting = self.phase.startswith("waiting for you")
        spin = "⏸" if waiting else SPINNER[self.frame]
        verb = "" if waiting else f"{self.verb}… "
        step, turn = now - getattr(self, "step_t0", self.t0), now - self.t0
        turn_txt = f"{turn / 60:.0f}m" if turn >= 90 else f"{turn:.0f}s"
        timing = f"step {step:.0f}s · turn {turn_txt}" if turn - step > 2 else f"{turn:.0f}s"
        self.label.setText(f"{spin} {verb}<span style='color:gray'>({self.phase} · {timing})</span>")


# (label, CLI permission mode, tool-name prefixes approved without asking)
PERMISSION_MODES = [
    ("Ask every time", "default", []),
    ("Auto-accept file edits", "acceptEdits", []),
    ("Trust CAD tools + file edits", "acceptEdits", ["mcp__plugin_"]),
    ("Plan only (no changes)", "plan", []),
]
DEFAULT_PERMISSION = "Trust CAD tools + file edits"


# Geometry files are opened by the agent with CAD tools, so they go in as @path mentions;
# everything else is attached and converted by the engine (images, PDFs, Office, text).
CAD_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".stl", ".obj", ".fcstd", ".brep", ".brp",
                ".dxf", ".dwg", ".3mf", ".ply", ".off", ".catpart", ".catproduct", ".sldprt", ".x_t"}
LONG_PASTE_CHARS = 4000  # like the Claude desktop app: long pasted text becomes an attachment


class ChatInput(QtWidgets.QPlainTextEdit):
    """Message box that takes dropped or pasted files, like the Claude desktop app."""

    files_added = QtCore.Signal(list)  # file paths to attach

    def canInsertFromMimeData(self, source) -> bool:
        return source.hasUrls() or source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        if source.hasUrls():
            files, mentions = [], []
            for url in source.urls():
                path = url.toLocalFile()
                if not path or Path(path).is_dir():
                    continue
                if Path(path).suffix.lower() in CAD_SUFFIXES:
                    mentions.append("@" + (f'"{path}"' if " " in path else path))
                else:
                    files.append(path)
            if files:
                self.files_added.emit(files)
            if mentions:
                self.insertPlainText(" ".join(mentions) + " ")
            if files or mentions:
                return
        if source.hasImage():  # e.g. a screenshot pasted from the snipping tool
            self.files_added.emit([self._save_paste("png", lambda p: QtGui.QImage(source.imageData()).save(p))])
            return
        if source.hasText() and len(source.text()) > LONG_PASTE_CHARS:
            text = source.text()
            self.files_added.emit([self._save_paste("txt", lambda p: Path(p).write_text(text, encoding="utf-8"))])
            return
        super().insertFromMimeData(source)

    @staticmethod
    def _save_paste(ext: str, writer) -> str:
        ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        path = str(ATTACH_DIR / f"pasted-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.{ext}")
        writer(path)
        return path


class ChatWidget(QtWidgets.QWidget):
    def __init__(self, engine: "EngineClient", panel: "ChatPanel", host_name: str = "standalone",
                 host_env: dict | None = None, host_capture=None, parent=None):
        """One chat (one tab). host_capture: optional callable(path) that saves the host's
        3D view as an image."""
        super().__init__(parent)
        self.panel = panel
        self.host_name = host_name
        self.host_env = host_env or {}
        self.host_capture = host_capture
        self.backend_session_id: str | None = None  # the CLI's id, used to resume
        self.resume_id: str | None = None
        self.attachments: list[str] = []
        self.mcp_servers: list[dict] = []
        self.engine_commands: list[dict] = []
        self.stream_md = ""
        self.stream_pos = 0
        self.session: str | None = None
        self.session_ready = False
        self.pending_prompt: str | None = None
        self.in_text = False  # currently streaming assistant text
        self._session_backend: str | None = None
        self.think_buf: list[str] = []
        self.think_t0 = 0.0
        self.thoughts: list[str] = []  # finished thinking blocks, opened from transcript links
        self.first_turn = True
        self.busy = False
        self.session_effort: str | None = None
        self.engine_ready = False
        self.mcp_text = "MCP connecting…"
        self.state = self._load_state()

        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._render_stream)
        self.engine = engine
        self.thinking_levels: list[str] = []
        self._build_ui()

    def selected_model(self) -> tuple[str, str]:
        """(backend, model) of the model picker. Stored as "backend|model": PySide6 turns
        tuples into lists, so findData() with a tuple never matches."""
        backend, _, model = (self.model.currentData() or "|").partition("|")
        return backend, model

    # ---------- UI ----------
    def _build_ui(self) -> None:
        default_cwd = ROOT / "sandbox" / "workdir"
        default_cwd.mkdir(parents=True, exist_ok=True)  # not in git; a fresh clone needs it
        cwd = self.state.get("cwd")
        self.folder = QtWidgets.QLineEdit(cwd if cwd and Path(cwd).is_dir() else str(default_cwd))
        browse = QtWidgets.QPushButton("…")
        browse.setFixedWidth(28)
        browse.clicked.connect(self.pick_folder)
        self.folder.editingFinished.connect(self.reset_session)

        self.model = QtWidgets.QComboBox()
        for backend, cfg in CONFIG["backends"].items():
            for m in cfg["models"]:
                self.model.addItem(f"{m}  ({cfg.get('label', backend)})", f"{backend}|{m}")
        want = self.state.get("model", (CONFIG["default"]["backend"], CONFIG["default"]["model"]))
        idx = self.model.findData("|".join(want))
        if idx >= 0:
            self.model.setCurrentIndex(idx)
        self.model.currentIndexChanged.connect(self.on_model_changed)

        new_btn = QtWidgets.QPushButton("New chat")
        new_btn.clicked.connect(self.new_chat)

        self.unload_btn = QtWidgets.QPushButton("Unload model")
        self.unload_btn.setToolTip("Free the GPU: evict local models from VRAM now. "
                                   "They reload automatically on the next message.")
        self.unload_btn.clicked.connect(self.unload_models)
        self.vram = QtWidgets.QLabel("VRAM: –")
        self.vram.setStyleSheet("color: gray; font-size: 11px;")

        top = QtWidgets.QGridLayout()
        top.addWidget(QtWidgets.QLabel("Folder"), 0, 0)
        top.addWidget(self.folder, 0, 1)
        top.addWidget(browse, 0, 2)
        top.addWidget(QtWidgets.QLabel("Model"), 1, 0)
        top.addWidget(self.model, 1, 1)
        top.addWidget(new_btn, 1, 2)
        top.addWidget(self.vram, 2, 1)
        top.addWidget(self.unload_btn, 2, 2)

        self.perm = QtWidgets.QComboBox()
        for label, mode, allow in PERMISSION_MODES:
            self.perm.addItem(label, (mode, allow))
        self.perm.setToolTip(
            "Ask every time: every file edit, shell command and CAD action needs approval.\n"
            "Auto-accept file edits: edits in the working folder run without asking.\n"
            "Trust CAD tools + file edits: CAD actions and edits run without asking; shell commands still ask.\n"
            "Plan only: the agent may read and plan but not change anything.")
        idx = self.perm.findText(self.state.get("permissions", DEFAULT_PERMISSION))
        self.perm.setCurrentIndex(max(idx, 0))
        self.perm.currentIndexChanged.connect(self.on_permissions_changed)
        top.addWidget(QtWidgets.QLabel("Allow"), 3, 0)
        top.addWidget(self.perm, 3, 1, 1, 2)

        tools = QtWidgets.QHBoxLayout()
        for label, tip, slot in (("History", "Resume an earlier chat", self.open_history),
                                 ("Save", "Save this chat to load it into other chats later (/save)", self.save_chat),
                                 ("Load\u2026", "Attach saved chats to the next message as context (/load)",
                                  self.load_context),
                                 ("Skills && MCP", "Manage skills, commands, subagents and MCP servers",
                                  self.open_manager)):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(QtWidgets.QLabel("Thinking"))
        self.think = QtWidgets.QComboBox()
        self.think.setToolTip("How much the model thinks before acting. Lower is faster and\n"
                              "less likely to overthink; the levels come from the model itself.")
        self.think.currentIndexChanged.connect(self.on_thinking_changed)
        tools.addWidget(self.think)
        top.addLayout(tools, 4, 1, 1, 2)

        # Cloud GPU row: only shown when the selected model runs on a rented GPU.
        self.cloud_row = QtWidgets.QWidget()
        row = QtWidgets.QGridLayout(self.cloud_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.instance_id = QtWidgets.QLineEdit(str(_recorded_instance() or ""))
        self.instance_id.setPlaceholderText("instance ID from vast.ai")
        self.instance_id.setValidator(QtGui.QIntValidator(1, 2_000_000_000))
        self.instance_id.returnPressed.connect(self.connect_cloud)
        self.connect_btn = connect = QtWidgets.QPushButton("Connect")
        connect.clicked.connect(self.connect_or_disconnect)
        self.instance_id.textChanged.connect(lambda _t: self._update_connect_button())
        rent = QtWidgets.QPushButton("Rent\u2026")
        rent.setToolTip("Rent a GPU from the current Vast offers (asks before billing starts)")
        rent.clicked.connect(self.rent_cloud)
        self.cloud_msg = QtWidgets.QLabel()
        self.cloud_msg.setWordWrap(True)
        self.cloud_msg.setStyleSheet("color: #c60; font-size: 11px;")
        row.addWidget(QtWidgets.QLabel("Instance"), 0, 0)
        row.addWidget(self.instance_id, 0, 1)
        row.addWidget(connect, 0, 2)
        row.addWidget(rent, 0, 3)
        row.addWidget(self.cloud_msg, 1, 0, 1, 4)
        self.pull_bar = QtWidgets.QProgressBar()  # model download on the cloud GPU
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setFixedHeight(12)
        self.pull_bar.setTextVisible(False)
        self.pull_bar.hide()
        row.addWidget(self.pull_bar, 2, 0, 1, 4)
        top.addWidget(self.cloud_row, 5, 0, 1, 3)
        self._update_cloud_row()
        self._update_connect_button()

        self.transcript = QtWidgets.QTextBrowser()
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self._on_link)

        self.activity = ActivityBar(show_thinking=self.state.get("show_thinking", False))
        self.activity.toggle.toggled.connect(lambda _on: self._save_state())

        self.input = ChatInput()
        self.input.files_added.connect(self._add_files)
        self.input.setPlaceholderText("Message, / for commands, or drop files and images here …  (Ctrl+Enter to send)")
        self.input.setFixedHeight(80)
        QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.input, activated=self.send_prompt)
        self.palette = CommandPalette(self.input)
        self.palette.set_commands([], LOCAL_COMMANDS)

        self.attach_label = QtWidgets.QLabel()
        self.attach_label.setStyleSheet("color: #2a6; font-size: 11px;")
        self.attach_label.hide()
        self.clear_attach = QtWidgets.QToolButton()
        self.clear_attach.setText("\u2715")
        self.clear_attach.setToolTip("Remove attachments")
        self.clear_attach.setAutoRaise(True)
        self.clear_attach.clicked.connect(self._clear_attachments)
        self.clear_attach.hide()
        self.view_btn = QtWidgets.QPushButton("\U0001f4f7 View")
        self.view_btn.setToolTip("Attach a screenshot of the 3D view to the next message")
        self.view_btn.clicked.connect(self.attach_view)
        self.view_btn.setEnabled(self.host_capture is not None)

        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self.send_prompt)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.interrupt)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.view_btn)
        buttons.addWidget(self.attach_label)
        buttons.addWidget(self.clear_attach)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.send_btn)

        self.status = QtWidgets.QLabel("starting engine…")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: gray; font-size: 11px;")

        self.ctx = QtWidgets.QProgressBar()
        self.ctx.setRange(0, 100)
        self.ctx.setValue(0)
        self.ctx.setFixedHeight(14)
        self.ctx.setTextVisible(True)
        self.ctx.setFormat("context –")
        self.ctx.setStyleSheet("QProgressBar { font-size: 10px; }")

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.transcript, 1)
        lay.addWidget(self.activity)
        lay.addWidget(self.palette)
        lay.addWidget(self.input)
        lay.addLayout(buttons)
        lay.addWidget(self.ctx)
        lay.addWidget(self.status)

    def pick_folder(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Working folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self.reset_session()

    def is_cloud(self) -> bool:
        return bool(CONFIG["backends"].get(self.selected_model()[0], {}).get("tunnel"))

    def _update_cloud_row(self) -> None:
        self.cloud_row.setVisible(self.is_cloud())
        self.unload_btn.setEnabled(not self.is_cloud())
        self.unload_btn.setToolTip(
            "The cloud GPU keeps the model loaded while it runs; use Destroy GPU to stop billing."
            if self.is_cloud() else
            "Free the GPU: evict local models from VRAM now. They reload automatically on the next message.")
        if self.is_cloud():
            self.show_cloud_gpu(self.panel.cloud if hasattr(self.panel, "cloud") else {})
        else:
            self.engine.send({"type": "model.status"})
        if self.is_cloud() and not _recorded_instance():
            self.cloud_msg.setText("Start an instance on vast.ai and enter its ID, or use Rent\u2026")

    def _update_connect_button(self) -> None:
        connected = _connected_instance()
        typed = self.instance_id.text().strip()
        if connected and str(connected) == typed:
            self.connect_btn.setText("Disconnect")
            self.connect_btn.setToolTip("Close the tunnel and stop using this GPU in chats.\n"
                                        "The instance keeps running (and billing) until you Destroy it.")
        else:
            self.connect_btn.setText("Connect")
            self.connect_btn.setToolTip("Connect to a running Vast instance: checks SSH, starts Ollama with the\n"
                                        "right settings, downloads the model if needed, opens the tunnel")

    def connect_or_disconnect(self) -> None:
        if self.connect_btn.text() == "Disconnect":
            self.engine.send({"type": "cloud.detach"})
        else:
            self.connect_cloud()

    def connect_cloud(self) -> None:
        text = self.instance_id.text().strip()
        if not text:
            self.cloud_msg.setText("Enter the instance ID shown on vast.ai (Instances page).")
            return
        self.cloud_msg.setText(f"connecting to instance {text}\u2026")
        self.panel.cloud_requester = self
        self.engine.send({"type": "cloud.attach", "instance_id": int(text)})

    def rent_cloud(self) -> None:
        self.cloud_msg.setText("searching Vast offers\u2026")
        self.panel.cloud_requester = self
        self.engine.send({"type": "cloud.offers"})

    def _show_offers(self, offers: list) -> None:
        if not offers:
            self.cloud_msg.setText("No suitable offers right now.")
            return
        dlg = RentDialog(offers, CONFIG["backends"].get("vast", {}).get("home_region", "Europe"), self)
        self.cloud_msg.setText("")
        if not dlg.exec() or not dlg.chosen:
            return
        o = dlg.chosen
        if QtWidgets.QMessageBox.question(
                self, "Confirm rental",
                f"Rent {o['gpu']} in {o['where']} at ${o['price_h']:.3f}/hour?\n\n"
                f"Billing starts now and continues until you destroy it.") != QtWidgets.QMessageBox.Yes:
            self.cloud_msg.setText("")
            return
        self.cloud_msg.setText(f"renting {o['gpu']} ({o['where']})\u2026")
        self.engine.send({"type": "cloud.create", "offer_id": o["id"]})

    def on_cloud_event(self, m: dict) -> None:
        t = m["type"]
        if t == "cloud.detached":
            self._update_connect_button()
            self.cloud_msg.setStyleSheet("color: #c60; font-size: 11px;")
            self.cloud_msg.setText("disconnected \u2014 the instance is still running (and billing); "
                                   "Connect to use it again, or Destroy GPU")
            if self.is_cloud() and self.session:
                self.close_session()
                self.session_ready = False
                self.status.setText("no cloud GPU connected: enter an instance ID and press Connect")
            return
        if t == "cloud.progress":
            if m.get("instance_id"):
                self.instance_id.setText(str(m["instance_id"]))
            self.cloud_msg.setText(m.get("text", ""))
            if m.get("percent") is not None:
                self.pull_bar.setValue(m["percent"])
                self.pull_bar.show()
            if m.get("ready"):
                self.pull_bar.hide()
            if m.get("ready") and self.is_cloud():
                self.reload_session("cloud model ready")
        elif t == "cloud.attached":
            self.cloud_msg.setText(("\u2713 " if m.get("ok") else "\u2717 ") + m.get("message", ""))
            self.cloud_msg.setStyleSheet(f"color: {'#2a6' if m.get('ok') else '#b00'}; font-size: 11px;")
            self._update_connect_button()
            if m.get("ok"):
                self.instance_id.setText(str(_recorded_instance() or self.instance_id.text()))
                if self.is_cloud() and m.get("model_state") == "present":
                    self.reload_session("connected to the cloud GPU")

    def on_model_changed(self) -> None:
        backend, model = self.selected_model()
        self._save_state()
        self._update_cloud_row()
        self.request_model_info()
        if self.session and self.session_ready and backend == self._session_backend:
            self.engine.send({"type": "session.set", "session": self.session, "model": model})
            self._note(f"model → {model}")
        else:
            self.reset_session()

    def request_model_info(self) -> None:
        backend, model = self.selected_model()
        self.engine.send({"type": "model.info", "backend": backend, "model": model})

    def _on_model_info(self, m: dict) -> None:
        if (m.get("backend"), m.get("model")) != self.selected_model():
            return
        self.thinking_levels = m.get("levels") or []
        if not self.thinking_levels:
            # The model could not be asked (e.g. Ollama still starting): say so and retry,
            # instead of pretending the model only supports "off".
            self.think.blockSignals(True)
            self.think.clear()
            self.think.addItem("unavailable", None)
            self.think.setToolTip(f"Could not read the model's thinking levels, retrying every 10 s.\n"
                                  f"{m.get('error') or ''}")
            self.think.blockSignals(False)
            QtCore.QTimer.singleShot(10000, lambda: self.thinking_levels or self.request_model_info())
            if not self.session and self.engine_ready:
                self._start_session()  # the chat still works at the model's default level
            return
        self.think.setToolTip("How much the model thinks before acting. Lower is faster and\n"
                              "less likely to overthink; the levels come from the model itself.")
        key = f"{m['backend']}|{m['model']}"
        saved = self.state.get("thinking", {}).get(key)
        preferred = CONFIG.get("default_thinking", {}).get(m["backend"])
        choice = next((c for c in (saved, preferred, m.get("default")) if c in self.thinking_levels),
                      self.thinking_levels[0] if self.thinking_levels else None)
        self.think.blockSignals(True)
        self.think.clear()
        for level in self.thinking_levels:
            label = level + (" (model default)" if level == m.get("default") else "")
            self.think.addItem(label, level)
        if choice is not None:
            self.think.setCurrentIndex(self.think.findData(choice))
        self.think.blockSignals(False)
        if not self.session and self.engine_ready:
            self._start_session()  # warm start (deferred from "ready")
        elif self.session and self.session_effort != self.current_effort():
            self.reload_session(f"thinking \u2192 {self.current_effort()}")

    def current_effort(self) -> str | None:
        return self.think.currentData()

    def on_thinking_changed(self) -> None:
        level = self.current_effort()
        if level is None:
            return
        backend, model = self.selected_model()
        self.state.setdefault("thinking", {})[f"{backend}|{model}"] = level
        self._save_state()
        if self.session and self.session_effort != level:
            self.reload_session(f"thinking \u2192 {level}")

    def reload_session(self, why: str) -> None:
        """Restart the session with new settings, continuing the conversation if there is one."""
        if self.backend_session_id and not self.first_turn:
            self.resume_id = self.backend_session_id
        self._note(why)
        self.reset_session()

    def on_permissions_changed(self) -> None:
        self._save_state()
        mode, allow = self.perm.currentData()
        if self.session and self.session_ready:
            self.engine.send({"type": "session.set", "session": self.session,
                              "permission_mode": mode, "auto_allow": allow})
            self._note(f"permissions → {self.perm.currentText()}")

    def unload_models(self) -> None:
        self.engine.send({"type": "model.unload"})

    def new_chat(self) -> None:
        """A new chat opens in a new tab; this one keeps its conversation."""
        self.panel.new_tab()

    def clear_chat(self) -> None:
        self.resume_id = None
        self.backend_session_id = None
        self.reset_session()
        self.transcript.clear()
        self.thoughts = []

    def open_history(self) -> None:
        self.panel.history_requester = self
        self.engine.send({"type": "sessions.list"})  # answered by a "sessions" event

    def _show_history(self, items: list) -> None:
        dlg = HistoryDialog(items, self.folder.text(), self)
        if dlg.exec() and dlg.chosen:
            self.resume(dlg.chosen, items)

    def resume(self, backend_session_id: str, items: list | None = None) -> None:
        for it in items or []:
            if it["backend_session_id"] == backend_session_id and it.get("cwd"):
                self.folder.setText(it["cwd"])
        self.transcript.clear()
        self.thoughts = []
        self._note("resuming an earlier chat…")
        self.resume_id = backend_session_id
        self.backend_session_id = backend_session_id
        self.reset_session()  # starts a session that continues the old one
        self.engine.send({"type": "session.history", "session": self.session,
                          "backend_session_id": backend_session_id})

    def _render_history(self, items: list) -> None:
        self.transcript.clear()
        for it in items:
            k = it["kind"]
            if k == "user":
                self._append(f"<p><b>You:</b> {html.escape(it['text'])}</p>")
            elif k == "assistant":
                self._append("<p><b>Agent:</b></p>" + md_to_html(it["text"]))
            elif k == "thinking":
                self.thoughts.append(it["text"])
                preview = html.escape(it["text"][:90].replace("\n", " "))
                self._append(f"<p style='color:gray; font-size:11px'>\U0001f4ad "
                             f"<a href='thinking:{len(self.thoughts) - 1}' style='color:gray'>Thought</a>"
                             f" \u00b7 <i>{preview}</i></p>")
            elif k == "tool":
                self._tool_line(it["name"], it.get("input"), None)
            elif k == "tool_result":
                self._result_line(it.get("content"), it.get("is_error"))
        self._note("— resumed; continue the conversation below —")

    def save_chat(self) -> None:
        if not self.backend_session_id or self.first_turn:
            self._note("nothing to save yet: send a message first")
            return
        i = self.panel.tabs.indexOf(self)
        default = self.panel.tabs.tabText(i).lstrip("\u25cf ").rstrip("\u2026").strip() if i >= 0 else "chat"
        title, ok = QtWidgets.QInputDialog.getText(self, "Save chat", "Name:", text=default)
        if not ok or not title.strip():
            return
        note, ok = QtWidgets.QInputDialog.getText(self, "Save chat", "Note (optional):")
        backend, model = self.selected_model()
        self.engine.send({"type": "session.save", "session": self.session,
                          "backend_session_id": self.backend_session_id, "title": title.strip(),
                          "note": note.strip() if ok else "",
                          "meta": {"model": model, "backend": backend, "cwd": self.folder.text()}})

    def load_context(self) -> None:
        self.panel.saved_requester = self
        self.engine.send({"type": "saved.list", "session": self.session})

    def _show_saved(self, items: list) -> None:
        dlg = LoadContextDialog(items, self)
        if not dlg.exec() or not dlg.paths:
            return
        self._add_files(dlg.paths)
        if not self.input.toPlainText().strip():
            self.input.setPlainText("Use the attached earlier chat(s) as context. ")
            cur = self.input.textCursor()
            cur.movePosition(QtGui.QTextCursor.End)
            self.input.setTextCursor(cur)
        self.input.setFocus()

    def open_manager(self) -> None:
        packs = [ROOT / p for p in CONFIG["packs"]]
        dlg = ManagerDialog(packs, ROOT / CONFIG["userplugins"], self.mcp_servers, self)
        dlg.ask_agent.connect(self._prefill)
        dlg.exec()
        if dlg.dirty:
            self.reload_plugins()

    def _prefill(self, text: str) -> None:
        self.input.setPlainText(text)
        self.input.setFocus()

    def reload_plugins(self) -> None:
        """Pick up changed skills and MCP servers; keep the conversation if there is one."""
        self.reload_session("reloading skills and MCP servers\u2026")

    # ---------- attachments ----------
    def attach_view(self) -> None:
        if not self.host_capture:
            return
        ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        path = ATTACH_DIR / f"view-{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            self.host_capture(str(path))
        except Exception as e:
            self._note(f"could not capture the view: {e}", "#b00")
            return
        self.attachments.append(str(path))
        self._show_attachments()

    def _show_attachments(self) -> None:
        n = len(self.attachments)
        names = ", ".join(Path(a).name for a in self.attachments)
        self.attach_label.setText(f"\U0001f4ce {names[:60]}{'…' if len(names) > 60 else ''}" if n else "")
        self.attach_label.setVisible(bool(n))
        self.clear_attach.setVisible(bool(n))
        if n:
            self.attach_label.setToolTip("\n".join(self.attachments))

    def _add_files(self, paths: list) -> None:
        self.attachments.extend(p for p in paths if p not in self.attachments)
        self._show_attachments()

    def _clear_attachments(self) -> None:
        self.attachments = []
        self._show_attachments()

    def _run_local_command(self, text: str) -> bool:
        name = text[1:].strip().split()[0] if text.startswith("/") and len(text) > 1 else ""
        actions = {"new": self.new_chat, "clear": self.clear_chat, "history": self.open_history,
                   "save": self.save_chat, "load": self.load_context,
                   "skills": self.open_manager,
                   "view": self.attach_view, "unload": self.unload_models}
        if name in actions:
            actions[name]()
            return True
        return False

    def reset_session(self) -> None:
        self._save_state()
        if self.session:
            self.engine.send({"type": "session.close", "session": self.session})
            self._note("— new session —")
        self.session = None
        self.session_ready = False
        self.first_turn = True
        if self.engine_ready:
            self._start_session()

    # ---------- sending ----------
    def send_prompt(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.palette.hide()
        self.input.clear()
        if self._run_local_command(text):
            return
        attachments = [{"kind": "file", "path": a} for a in self.attachments]
        self._clear_attachments()
        self.panel.set_title(self, text)
        clip = "".join(f"<br><span style='color:#2a6'>\U0001f4ce {html.escape(Path(a['path']).name)}</span>"
                       for a in attachments)
        self._append(f"<p><b>You:</b> {html.escape(text)}{clip}</p>")
        self._busy(True)
        backend, _model = self.selected_model()
        cold = self.first_turn and backend == "ollama" and "free" in self.vram.text()
        self.activity.start("loading model" if cold else "waiting for model")
        msg = {"type": "prompt", "session": self.session, "text": text, "attachments": attachments}
        if self.session and self.session_ready:
            self.engine.send(msg)
        else:
            self.pending_prompt = msg
            if not self.session:
                self._start_session()

    def _start_session(self) -> None:
        backend, model = self.selected_model()
        if self.is_cloud() and not _connected_instance():
            self.status.setText("no cloud GPU connected: enter an instance ID and press Connect")
            return
        self._session_backend = backend
        self.session = uuid.uuid4().hex[:8]
        self.engine.send({"type": "session.start", "session": self.session, "cwd": self.folder.text(),
                          "backend": backend, "model": model, "env": self.host_env,
                          "permission_mode": self.perm.currentData()[0],
                          "auto_allow": self.perm.currentData()[1], "resume": self.resume_id,
                          "effort": self.current_effort()})
        self.session_effort = self.current_effort()
        self.resume_id = None
        self.status.setText(f"starting session in {self.folder.text()} with {model}…")

    def interrupt(self) -> None:
        if self.session:
            self.engine.send({"type": "interrupt", "session": self.session})

    # ---------- events ----------
    def on_event(self, m: dict) -> None:
        t = m.get("type")
        if t in ("model.status", "model.unloaded"):
            self._on_model_event(m)
            return
        if t == "sessions":
            self._show_history(m.get("items") or [])
            return
        if t == "model.info":
            self._on_model_info(m)
            return
        if m.get("session") not in (None, self.session):
            return  # stale event from a closed session
        if t != "thinking.delta":
            self._end_thinking()
        if t != "text.delta":
            self._end_text()
        if t == "ready":
            self.engine_ready = True
            self.status.setText(f"engine {m['engine']} ready · host: {self.host_name}")
            # Warm start once the thinking levels are known (see _on_model_info), so the
            # session is not started at the model default and then restarted.
            self.request_model_info()
            QtCore.QTimer.singleShot(4000, lambda: self.session or self._start_session())
        elif t == "session.mcp":
            self._on_mcp(m.get("servers") or [])
        elif t == "session.info":
            self.engine_commands = m.get("commands") or []
            self.palette.set_commands(self.engine_commands, LOCAL_COMMANDS)
        elif t == "context":
            self._on_context(m)
        elif t == "history":
            self._render_history(m.get("items") or [])
        elif t == "session.saved":
            self._note(f"saved to {m.get('path')}  (load it into any chat with Load\u2026 or /load)", "#2a6")
        elif t == "saved.list":
            self._show_saved(m.get("items") or [])
        elif t == "session.started":
            self.session_ready = True
            if self.pending_prompt:
                self.engine.send({**self.pending_prompt, "session": self.session})
                self.pending_prompt = None
        elif t == "session.catalog":
            c = m["catalog"]
            self.backend_session_id = m.get("backend_session_id") or self.backend_session_id
            self.status.setText(f"{c.get('model')} · {len(c.get('skills') or [])} skills · "
                                f"{len(c.get('agents') or [])} agents · {self.mcp_text} · {self.host_name}")
        elif t == "tool.writing":
            name = pretty_tool(m.get("name") or "tool")
            if not m.get("chars"):
                self.activity.add_thinking(f"\n— writing {name} —\n")
            else:
                # Tool input streams as JSON; unescape the common sequences so code reads as code.
                self.activity.add_thinking(m.get("text", "").replace("\\n", "\n").replace('\\"', '"')
                                           .replace("\\t", "    "))
            self.activity.set_phase(f"writing {name}… {m.get('chars', 0):,} chars")
        elif t == "thinking.delta":
            if not self.think_buf:
                self.think_t0 = time.monotonic()
                self.activity.new_thought()
            self.think_buf.append(m["text"])
            self.activity.add_thinking(m["text"])
            self.activity.set_phase("thinking")
        elif t == "text.delta":
            self.activity.set_phase("writing")
            self._stream_text(m["text"], m.get("parent"))
        elif t == "tool.start":
            self.activity.set_phase(f"running {pretty_tool(m['name'])}")
            self._tool_line(m["name"], m.get("input"), m.get("parent"))
        elif t == "tool.result":
            self.activity.set_phase("waiting for model")
            self._result_line(m.get("content"), m.get("is_error"))
        elif t == "todo.update":
            items = "".join(f"<li>{'☑' if td.get('status') == 'completed' else '☐'} "
                            f"{html.escape(td.get('content', ''))}</li>" for td in m["todos"])
            self._append(f"<ul style='color:#555'>{items}</ul>")
        elif t == "permission.request":
            self.activity.set_phase(f"waiting for your approval of {pretty_tool(m['tool'])}")
            self._ask_permission(m)
            self.activity.set_phase("working")
        elif t == "question.request":
            self.activity.set_phase("waiting for your answer")
            self._ask_questions(m)
            self.activity.set_phase("working")
        elif t == "turn.end":
            self.first_turn = False
            self.backend_session_id = m.get("backend_session_id") or self.backend_session_id
            self.activity.stop()
            self._busy(False)
            self.engine.send({"type": "model.status"})
            u = m.get("usage") or {}
            secs = (m.get("duration_ms") or 0) / 1000
            self._note(f"{m.get('status')} · {m.get('turns')} turns · {secs:.1f}s · "
                       f"in {u.get('input_tokens', '?')} / out {u.get('output_tokens', '?')} tokens")
        elif t == "error":
            self.activity.stop()
            self._busy(False)
            self._note(f"error: {m.get('message')}", "#b00")

    def show_cloud_gpu(self, m: dict) -> None:
        """For cloud tabs the VRAM line describes the rented GPU, not the local one."""
        if not self.is_cloud():
            return
        if m.get("running") and "gpu_util" in m:
            self.vram.setText(f"GPU (cloud): {m['gpu_mem_used_gb']:.0f}/{m['gpu_mem_total_gb']} GB \u00b7 "
                              f"{m['gpu_util']}% busy \u00b7 model stays loaded")
            self.vram.setStyleSheet("color: #c60; font-size: 11px;")
        elif m.get("running"):
            self.vram.setText("GPU (cloud): running")
        else:
            self.vram.setText("GPU (cloud): none")
            self.vram.setStyleSheet("color: gray; font-size: 11px;")

    def _on_model_event(self, m: dict) -> None:
        if m["type"] == "model.status" and self.is_cloud():
            return  # local GPU status; this tab shows the cloud GPU instead
        if m["type"] == "model.status":
            loaded = m.get("loaded") or []
            if loaded:
                gb = sum(x.get("vram_bytes", 0) for x in loaded) / 1e9
                self.vram.setText("VRAM: " + ", ".join(x["name"] for x in loaded) + f" ({gb:.1f} GB)")
                self.vram.setStyleSheet("color: #c60; font-size: 11px;")
            else:
                self.vram.setText("VRAM: free")
                self.vram.setStyleSheet("color: gray; font-size: 11px;")
        else:
            if m.get("models"):
                self._note(f"unloaded {', '.join(m['models'])} ({m.get('reason')})")
            elif m.get("reason") == "user request":
                self._note("no local model was loaded")
            self.engine.send({"type": "model.status"})

    def _on_mcp(self, servers: list) -> None:
        def short(name):
            return name.split(":")[-1]
        parts = []
        for srv in servers:
            if srv["status"] == "connected":
                parts.append(f"{short(srv['name'])} ✓ {srv.get('tools', 0)} tools")
            else:
                parts.append(f"{short(srv['name'])} ✗ {srv['status']}")
                self._note(f"MCP server {srv['name']} is {srv['status']}"
                           + (f": {srv['error']}" if srv.get("error") else ""), "#b00")
        self.mcp_text = "MCP " + (", ".join(parts) if parts else "none")
        self.status.setText(f"ready · {self.mcp_text} · {self.host_name}")

    def _tool_line(self, name: str, tool_input, parent) -> None:
        inp = json.dumps(tool_input, ensure_ascii=False)
        inp = inp if len(inp) < 160 else inp[:160] + "\u2026"
        indent = "&nbsp;&nbsp;&nbsp;&nbsp;" if parent else ""
        self._append(f"<p style='color:#2a6'>{indent}\u25b8 <b>{html.escape(pretty_tool(name))}</b> "
                     f"<span style='color:gray'>{html.escape(inp)}</span></p>")

    def _result_line(self, content, is_error) -> None:
        lines = str(content or "").strip().splitlines()
        first = lines[0][:160] if lines else ""
        colour = "#b00" if is_error else "gray"
        mark = "✗" if is_error else "✓"
        more = " …" if len(lines) > 1 else ""
        self._append(f"<p style='color:{colour}'>&nbsp;&nbsp;{mark} {html.escape(first)}{more}</p>")

    def _on_context(self, m: dict) -> None:
        used, cap, at = m.get("used") or 0, m.get("max") or 1, m.get("autocompact_at") or 0
        # The bar measures progress towards automatic compaction, which is what the user feels.
        limit = at or cap
        pct = min(100, round(100 * used / limit)) if limit else 0
        self.ctx.setValue(pct)
        self.ctx.setFormat(f"context {used / 1000:.1f}k / {cap / 1000:.0f}k \u00b7 "
                           f"summarises at {limit / 1000:.0f}k ({pct}%)")
        colour = "#b00" if pct >= 90 else "#c60" if pct >= 70 else "#2a6"
        self.ctx.setStyleSheet(f"QProgressBar {{ font-size: 10px; }} "
                               f"QProgressBar::chunk {{ background: {colour}; }}")
        tips = "\n".join(f"{c['name']}: {c['tokens']:,}" for c in m.get("categories") or [])
        self.ctx.setToolTip(tips)

    def _ask_permission(self, m: dict) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Allow tool?")
        box.setText(f"The agent wants to use <b>{html.escape(m['tool'])}</b>")
        box.setDetailedText(json.dumps(m.get("input"), indent=2, ensure_ascii=False))
        box.setInformativeText(json.dumps(m.get("input"), ensure_ascii=False)[:400])
        allow = box.addButton("Allow", QtWidgets.QMessageBox.AcceptRole)
        always = box.addButton("Always allow", QtWidgets.QMessageBox.AcceptRole)
        box.addButton("Deny", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        decision = "allow" if clicked in (allow, always) else "deny"
        self.engine.send({"type": "permission.reply", "request_id": m["request_id"],
                          "decision": decision, "remember": clicked is always})
        self._note(f"{decision} {pretty_tool(m['tool'])}")

    def _ask_questions(self, m: dict) -> None:
        answers = {}
        for q in m.get("questions", []):
            labels = [o.get("label", "") for o in q.get("options", [])]
            choice, ok = QtWidgets.QInputDialog.getItem(
                self, q.get("header", "Question"), q.get("question", ""), labels + ["Other…"], 0, False)
            if ok and choice == "Other…":
                choice, ok = QtWidgets.QInputDialog.getText(self, q.get("header", "Question"), q.get("question", ""))
            answers[q.get("question", "")] = choice if ok else ""
        self.engine.send({"type": "question.reply", "request_id": m["request_id"], "answers": answers})

    # ---------- transcript helpers ----------
    def _stream_text(self, text: str, parent) -> None:
        """Stream markdown: the message's HTML is re-rendered in place, at most ~12 times a second."""
        if not self.in_text:
            self._append(f"<p><b>{'Agent:' if not parent else '&nbsp;&nbsp;&nbsp;&nbsp;subagent:'}</b></p>")
            cur = self.transcript.textCursor()
            cur.movePosition(QtGui.QTextCursor.End)
            cur.insertBlock()  # the message body starts on its own line, below the header
            self.stream_pos = cur.position()
            self.stream_md = ""
            self.in_text = True
        self.stream_md += text
        if not self.render_timer.isActive():
            self.render_timer.start(80)

    def _render_stream(self) -> None:
        cur = self.transcript.textCursor()
        cur.setPosition(self.stream_pos)
        cur.movePosition(QtGui.QTextCursor.End, QtGui.QTextCursor.KeepAnchor)
        cur.removeSelectedText()
        cur.insertHtml(md_to_html(self.stream_md))
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _end_thinking(self) -> None:
        if not self.think_buf:
            return
        text = "".join(self.think_buf).strip()
        self.think_buf = []
        if not text:
            return
        self.thoughts.append(text)
        secs = time.monotonic() - self.think_t0
        preview = html.escape(text[:90].replace("\n", " ")) + ("…" if len(text) > 90 else "")
        self._append(f"<p style='color:gray; font-size:11px'>💭 <a href='thinking:{len(self.thoughts) - 1}' "
                     f"style='color:gray'>Thought for {max(secs, 1):.0f}s</a> · <i>{preview}</i></p>")

    def _on_link(self, url) -> None:
        if url.scheme() == "thinking":
            text = self.thoughts[int(url.path())]
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Model thinking")
            view = QtWidgets.QPlainTextEdit(text)
            view.setReadOnly(True)
            QtWidgets.QVBoxLayout(dlg).addWidget(view)
            dlg.resize(560, 420)
            dlg.show()
        else:
            QtGui.QDesktopServices.openUrl(url)

    def _end_text(self) -> None:
        if self.in_text:
            self.render_timer.stop()
            self._render_stream()
        self.in_text = False

    def _append(self, html_text: str) -> None:
        self.transcript.append(html_text)
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def _note(self, text: str, colour: str = "gray") -> None:
        self._append(f"<p style='color:{colour}; font-size:11px'>{html.escape(text)}</p>")

    def _busy(self, busy: bool) -> None:
        self.stop_btn.setEnabled(busy)
        self.send_btn.setEnabled(not busy)
        self.busy = busy
        self.panel.set_busy(self, busy)

    # ---------- state ----------
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(_read_state() | {
                "cwd": self.folder.text(),
                "model": list(self.selected_model()),
                "show_thinking": self.activity.toggle.isChecked() if hasattr(self, "activity") else False,
                "permissions": self.perm.currentText() if hasattr(self, "perm") else DEFAULT_PERMISSION,
                "thinking": self.state.get("thinking", {}),
            }), encoding="utf-8")
        except Exception:
            pass

    def close_session(self) -> None:
        self._save_state()
        if self.session:
            self.engine.send({"type": "session.close", "session": self.session})
            self.session = None


class ChatPanel(QtWidgets.QWidget):
    """Tabs of independent chats sharing one engine process. Each tab has its own session,
    so a new chat starts with a fresh context while earlier chats stay open."""

    def __init__(self, parent=None, host_name: str = "standalone", host_env: dict | None = None,
                 host_capture=None):
        super().__init__(parent)
        self.host = dict(host_name=host_name, host_env=host_env, host_capture=host_capture)
        self.ready_event: dict | None = None
        self.history_requester: ChatWidget | None = None
        self.cloud_requester: ChatWidget | None = None
        self.saved_requester: ChatWidget | None = None

        self.engine = EngineClient(self)
        self.engine.message.connect(self._route)
        self.engine.stopped.connect(lambda msg: [t._note(msg, "#b00") for t in self.chats()])

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        plus = QtWidgets.QToolButton()
        plus.setText("+")
        plus.setToolTip("New chat (fresh context); other chats stay open")
        plus.setAutoRaise(True)
        plus.clicked.connect(self.new_tab)
        self.tabs.setCornerWidget(plus, QtCore.Qt.TopRightCorner)
        # Cost guard: visible whenever a rented GPU exists, so it is never forgotten.
        self.cloud = {}
        self.cloud_label = QtWidgets.QLabel()
        self.cloud_label.setStyleSheet("color: #c60; font-weight: bold;")
        destroy = QtWidgets.QPushButton("Destroy GPU")
        destroy.setToolTip("Delete the rented instance now and stop billing")
        destroy.clicked.connect(self.destroy_cloud)
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setToolTip("Resume the stopped instance (the model is still on its disk)")
        self.start_btn.clicked.connect(self.start_cloud)
        self.start_btn.hide()
        self.cloud_bar = QtWidgets.QFrame()
        self.cloud_bar.setStyleSheet("QFrame { border: 1px solid #c60; border-radius: 4px; }")
        bar = QtWidgets.QHBoxLayout(self.cloud_bar)
        bar.setContentsMargins(6, 2, 6, 2)
        bar.addWidget(self.cloud_label, 1)
        bar.addWidget(self.start_btn)
        bar.addWidget(destroy)
        self.cloud_bar.hide()

        # Idle auto-destroy: warn, count down, destroy unless the user keeps it.
        self.idle_minutes = int(CONFIG["backends"].get("vast", {}).get("idle_destroy_minutes", 20))
        self.last_active = time.monotonic()
        self.countdown = 0
        self.idle_label = QtWidgets.QLabel()
        self.idle_label.setStyleSheet("color: #b00; font-weight: bold;")
        keep = QtWidgets.QPushButton("Keep running")
        keep.clicked.connect(self.keep_cloud)
        self.idle_bar = QtWidgets.QFrame()
        self.idle_bar.setStyleSheet("QFrame { border: 1px solid #b00; border-radius: 4px; }")
        ib = QtWidgets.QHBoxLayout(self.idle_bar)
        ib.setContentsMargins(6, 2, 6, 2)
        ib.addWidget(self.idle_label, 1)
        ib.addWidget(keep)
        self.idle_bar.hide()
        self.idle_timer = QtCore.QTimer(self)
        self.idle_timer.timeout.connect(self._idle_tick)
        self.idle_timer.start(1000)

        # Past chats, like the Claude desktop app's sidebar; collapsed by default (docks are narrow).
        self.sidebar = ChatSidebar()
        self.sidebar.open_session.connect(self.open_past_chat)
        self.sidebar.attach_saved.connect(self.attach_saved_chat)
        self.sidebar.save_session.connect(self.save_past_chat)
        self.sidebar.delete_session.connect(
            lambda bid: self.engine.send({"type": "session.delete", "backend_session_id": bid, "for": "sidebar"}))
        self.sidebar.refresh_requested.connect(self.refresh_sidebar)
        self.side_toggle = QtWidgets.QToolButton()
        self.side_toggle.setText("\u2630")
        self.side_toggle.setToolTip("Show or hide past chats")
        self.side_toggle.setCheckable(True)
        self.side_toggle.setAutoRaise(True)
        self.side_toggle.toggled.connect(self.show_sidebar)
        self.tabs.setCornerWidget(self.side_toggle, QtCore.Qt.TopLeftCorner)
        self.split = QtWidgets.QSplitter()
        self.split.addWidget(self.sidebar)
        self.split.addWidget(self.tabs)
        self.split.setStretchFactor(1, 1)
        self.split.setChildrenCollapsible(False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.cloud_bar)
        lay.addWidget(self.idle_bar)
        lay.addWidget(self.split)
        self.sidebar.setVisible(False)
        self.side_toggle.setChecked(bool(_read_state().get("sidebar_open", False)))
        self.side_refresh = QtCore.QTimer(self)
        self.side_refresh.timeout.connect(self.refresh_sidebar)
        self.side_refresh.start(120000)

        self.new_tab()
        self.engine.start()
        # Cheap poll of Ollama's /api/ps so every tab shows what holds the GPU.
        self.vram_timer = QtCore.QTimer(self)
        self.vram_timer.timeout.connect(lambda: self.engine.send({"type": "model.status"}))
        self.vram_timer.start(10000)
        self.cloud_timer = QtCore.QTimer(self)
        self.cloud_timer.timeout.connect(self.poll_cloud)
        self.cloud_timer.start(60000)
        QtCore.QTimer.singleShot(3000, self.poll_cloud)

    def poll_cloud(self) -> None:
        try:
            recorded = json.loads(VAST_STATE.read_text(encoding="utf-8")).get("instance_id")
        except (OSError, ValueError):
            recorded = None
        if recorded:
            self.engine.send({"type": "cloud.status"})
        else:
            self.cloud = {}
            self.cloud_bar.hide()

    def _on_cloud(self, m: dict) -> None:
        self.cloud = m if m.get("instance_id") else {}
        if not self.cloud:
            self.cloud_bar.hide()
            for tab in self.chats():
                tab.show_cloud_gpu({})
                tab._update_connect_button()
            if m.get("destroyed"):
                self.tabs.currentWidget()._note("cloud GPU destroyed; billing stopped")
            return
        state = m.get("state") or "starting"
        gpu = m.get("gpu") or "Vast GPU"
        if state == "stopped":
            text = f"\u2601 {gpu} \u00b7 stopped (storage only; model kept) \u00b7 ${m.get('cost_so_far', 0):.2f} so far"
        else:
            text = (f"\u2601 {gpu} \u00b7 {state} \u00b7 ${m.get('price_h', 0):.2f}/h \u00b7 "
                    f"{m.get('hours', 0):.1f} h \u00b7 ${m.get('cost_so_far', 0):.2f} so far")
            if "gpu_util" in m:
                text += (f"\n     GPU {m['gpu_util']}% \u00b7 {m['gpu_mem_used_gb']:.0f}/{m['gpu_mem_total_gb']} GB"
                         f" \u00b7 idle {m.get('idle_min', 0)} min (auto-destroy at {self.idle_minutes}"
                         + (f", instance stops itself at {m['watchdog_limit_min']}" if m.get("watchdog_limit_min") else "")
                         + ")")
        self.cloud_label.setText(text)
        self.start_btn.setVisible(state == "stopped")
        self.cloud_bar.show()
        for tab in self.chats():
            tab.show_cloud_gpu(m)

    def start_cloud(self) -> None:
        self.last_active = time.monotonic()
        self.engine.send({"type": "cloud.start", "instance_id": self.cloud.get("instance_id")})
        self.cloud_label.setText(self.cloud_label.text().split("\n")[0] + "  (starting\u2026)")

    def mark_cloud_active(self) -> None:
        self.last_active = time.monotonic()
        if self.countdown:
            self.keep_cloud()

    def keep_cloud(self) -> None:
        self.countdown = 0
        self.last_active = time.monotonic()
        self.idle_bar.hide()

    def _idle_tick(self) -> None:
        if not self.cloud.get("running"):
            self.countdown = 0
            self.idle_bar.hide()
            return
        if any(t.is_cloud() and t.busy for t in self.chats()):
            self.mark_cloud_active()
            return
        local_idle = (time.monotonic() - self.last_active) / 60
        # The instance's own idle counter also sees other windows or clients using the GPU.
        idle = min(local_idle, self.cloud.get("idle_min", local_idle))
        if not self.countdown and idle >= self.idle_minutes:
            self.countdown = 60
        if self.countdown:
            self.countdown -= 1
            self.idle_label.setText(f"Cloud GPU idle for {self.idle_minutes} min: destroying it in "
                                    f"{self.countdown} s to stop billing.")
            self.idle_bar.show()
            if self.countdown == 0:
                self.idle_bar.hide()
                self.engine.send({"type": "cloud.destroy", "instance_id": self.cloud.get("instance_id")})
                self.cloud["running"] = False

    def destroy_cloud(self) -> None:
        cost = self.cloud.get("cost_so_far")
        if QtWidgets.QMessageBox.question(
                self, "Destroy cloud GPU",
                f"Delete the Vast instance now? Billing stops; the downloaded model is lost "
                f"(${cost} so far).") == QtWidgets.QMessageBox.Yes:
            self.engine.send({"type": "cloud.destroy", "instance_id": self.cloud.get("instance_id")})

    # ---------- sidebar ----------
    def show_sidebar(self, on: bool) -> None:
        self.sidebar.setVisible(on)
        if on:
            self.split.setSizes([230, max(self.width() - 230, 300)])
            self.refresh_sidebar()
        _write_state(sidebar_open=on)

    def refresh_sidebar(self) -> None:
        if not self.sidebar.isVisible():
            return
        self.sidebar.set_open({t.backend_session_id for t in self.chats()})
        self.engine.send({"type": "sessions.list", "for": "sidebar", "limit": 200})
        self.engine.send({"type": "saved.list", "for": "sidebar"})

    def open_past_chat(self, backend_id: str, cwd: str) -> None:
        for tab in self.chats():
            if tab.backend_session_id == backend_id:
                self.tabs.setCurrentWidget(tab)
                return
        tab = self.new_tab()
        tab.resume(backend_id, [{"backend_session_id": backend_id, "cwd": cwd}])
        QtCore.QTimer.singleShot(500, self.refresh_sidebar)

    def attach_saved_chat(self, path: str) -> None:
        tab = self.tabs.currentWidget()
        tab._add_files([path])
        if not tab.input.toPlainText().strip():
            tab.input.setPlainText("Use the attached earlier chat as context. ")
        tab.input.setFocus()

    def save_past_chat(self, backend_id: str, title: str) -> None:
        title, ok = QtWidgets.QInputDialog.getText(self, "Save chat", "Name:", text=title)
        if ok and title.strip():
            self.engine.send({"type": "session.save", "backend_session_id": backend_id, "title": title.strip(),
                              "meta": {}, "for": "sidebar"})

    def chats(self) -> list[ChatWidget]:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def new_tab(self) -> ChatWidget:
        tab = ChatWidget(self.engine, self, **self.host)
        self.tabs.addTab(tab, "New chat")
        self.tabs.setCurrentWidget(tab)
        if self.ready_event:
            tab.on_event(self.ready_event)
        tab.input.setFocus()
        return tab

    def close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if getattr(tab, "busy", False) and QtWidgets.QMessageBox.question(
                self, "Close chat", "This chat is still working. Stop it and close?") != QtWidgets.QMessageBox.Yes:
            return
        tab.close_session()
        self.tabs.removeTab(index)
        tab.deleteLater()
        if not self.tabs.count():
            self.new_tab()

    def set_title(self, tab: ChatWidget, text: str) -> None:
        i = self.tabs.indexOf(tab)
        if i >= 0 and self.tabs.tabText(i).lstrip("\u25cf ") == "New chat" and not text.startswith("/"):
            title = " ".join(text.split())[:22]
            self.tabs.setTabText(i, title + ("\u2026" if len(text) > 22 else ""))
            self.tabs.setTabToolTip(i, text[:300])

    def set_busy(self, tab: ChatWidget, busy: bool) -> None:
        if tab.is_cloud():
            self.mark_cloud_active()
        if not busy:
            QtCore.QTimer.singleShot(1500, self.refresh_sidebar)
        i = self.tabs.indexOf(tab)
        if i < 0:
            return
        title = self.tabs.tabText(i).lstrip("\u25cf ")
        self.tabs.setTabText(i, ("\u25cf " if busy else "") + title)

    def _route(self, m: dict) -> None:
        t = m.get("type")
        if t == "ready":
            self.ready_event = m
            QtCore.QTimer.singleShot(1000, self.refresh_sidebar)
            for tab in self.chats():
                tab.on_event(m)
        elif t == "sessions" and m.get("for") == "sidebar":
            self.sidebar.set_sessions(m.get("items") or [])
        elif t == "saved.list" and m.get("for") == "sidebar":
            self.sidebar.set_saved(m.get("items") or [])
        elif t == "session.deleted":
            if not m.get("moved"):
                self.tabs.currentWidget()._note("that chat's file was not found (already deleted?)", "#b00")
            self.refresh_sidebar()
        elif t == "session.saved" and not m.get("session"):
            self.tabs.currentWidget().on_event(m)
            self.refresh_sidebar()
        elif t == "sessions":
            (self.history_requester or self.tabs.currentWidget()).on_event(m)
        elif t == "cloud.status":
            self._on_cloud(m)
        elif t == "saved.list" and not m.get("session"):
            (self.saved_requester or self.tabs.currentWidget()).on_event(m)
        elif t == "cloud.offers":
            (self.cloud_requester or self.tabs.currentWidget())._show_offers(m.get("offers") or [])
        elif t == "cloud.detached":
            for tab in self.chats():
                tab.on_cloud_event(m)
            self.poll_cloud()
        elif t in ("cloud.progress", "cloud.attached"):
            for tab in self.chats():
                if tab.is_cloud() or tab is self.cloud_requester:
                    tab.on_cloud_event(m)
            if t == "cloud.attached" or m.get("ready"):
                self.mark_cloud_active()
                self.poll_cloud()
        elif t in ("model.status", "model.info"):
            for tab in self.chats():
                tab.on_event(m)
        elif t == "model.unloaded":
            self.tabs.currentWidget().on_event(m)  # other tabs catch up on the next status poll
        elif m.get("session"):
            for tab in self.chats():
                if tab.session == m["session"]:
                    tab.on_event(m)
                    break
        else:  # engine-wide messages, e.g. errors without a session
            self.tabs.currentWidget().on_event(m)

    def shutdown(self) -> None:
        for tab in self.chats():
            tab._save_state()
        if self.cloud.get("running") and QtWidgets.QMessageBox.question(
                self, "Cloud GPU still running",
                f"Your {self.cloud.get('gpu') or 'Vast'} GPU is still running at "
                f"${self.cloud.get('price_h', 0):.2f}/h. Destroy it now?") == QtWidgets.QMessageBox.Yes:
            self.engine.send({"type": "cloud.destroy", "instance_id": self.cloud.get("instance_id")})
            QtCore.QThread.msleep(4000)  # let the engine finish the request before it is told to exit
        self.engine.shutdown()


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = ChatPanel(host_name="standalone")
    w.setWindowTitle("Agent chat")
    w.resize(560, 800)
    app.aboutToQuit.connect(w.shutdown)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
