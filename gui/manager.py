"""Skill, command, subagent and MCP-server manager.

Bundled packs are shown read-only; plugins under userplugins/ can be created,
edited and deleted. Everything is plain Claude Code plugin files on disk, so
anything made here also works in the Claude Code CLI.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from .qt import QtCore, QtGui, QtWidgets

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")

TEMPLATES = {
    "skill": ("skills/{name}/SKILL.md", """---
name: {name}
description: {description}
---
# {title}

Instructions for the agent: when to use this skill, the steps to follow,
FreeCAD property names or conventions it must respect, and how to check the result.
"""),
    "command": ("commands/{name}.md", """---
description: {description}
argument-hint: [what the user types after the command]
---
{description}

The user's arguments: $ARGUMENTS
"""),
    "agent": ("agents/{name}.md", """---
name: {name}
description: {description}
tools: Read, Grep, Glob
---
You are a specialist subagent. {description}
Report back concisely.
"""),
}


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class Plugin:
    def __init__(self, path: Path, editable: bool):
        self.path = path
        self.editable = editable
        meta = json.loads((path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.name = meta.get("name", path.name)

    def items(self) -> list[tuple[str, str, str, Path]]:
        """(kind, name, description, file) for every skill, command and agent."""
        out = []
        for f in sorted((self.path / "skills").glob("*/SKILL.md")):
            out.append(("skill", f.parent.name, _frontmatter(f).get("description", ""), f))
        for f in sorted((self.path / "commands").glob("*.md")):
            out.append(("command", f.stem, _frontmatter(f).get("description", ""), f))
        for f in sorted((self.path / "agents").glob("*.md")):
            fm = _frontmatter(f)
            out.append(("agent", fm.get("name", f.stem), fm.get("description", ""), f))
        return out

    def mcp_file(self) -> Path:
        return self.path / ".mcp.json"

    def mcp_servers(self) -> dict:
        try:
            return json.loads(self.mcp_file().read_text(encoding="utf-8")).get("mcpServers", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def save_mcp_servers(self, servers: dict) -> None:
        self.mcp_file().write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


class EditorDialog(QtWidgets.QDialog):
    def __init__(self, path: Path, editable: bool, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(("Edit " if editable else "View ") + str(path))
        self.resize(720, 560)
        self.text = QtWidgets.QPlainTextEdit(path.read_text(encoding="utf-8"))
        self.text.setReadOnly(not editable)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        buttons = QtWidgets.QDialogButtonBox(
            (QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel) if editable
            else QtWidgets.QDialogButtonBox.Close)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        if not editable:
            note = QtWidgets.QLabel("Bundled with the application pack: read-only. Copy it to your user "
                                    "plugin to customise it.")
            note.setStyleSheet("color: gray;")
            lay.addWidget(note)
        lay.addWidget(self.text)
        lay.addWidget(buttons)

    def _save(self) -> None:
        self.path.write_text(self.text.toPlainText(), encoding="utf-8")
        self.accept()


class McpDialog(QtWidgets.QDialog):
    def __init__(self, name: str = "", cfg: dict | None = None, parent=None):
        super().__init__(parent)
        cfg = cfg or {}
        self.setWindowTitle("MCP server")
        self.resize(560, 320)
        self.name = QtWidgets.QLineEdit(name)
        self.command = QtWidgets.QLineEdit(cfg.get("command", ""))
        self.command.setPlaceholderText("e.g. uvx, npx, or a full path to python.exe")
        self.args = QtWidgets.QLineEdit(" ".join(shlex.quote(a) for a in cfg.get("args", [])))
        self.args.setPlaceholderText("arguments, separated by spaces")
        self.env = QtWidgets.QPlainTextEdit("\n".join(f"{k}={v}" for k, v in cfg.get("env", {}).items()))
        self.env.setPlaceholderText("KEY=value, one per line (optional)")
        form = QtWidgets.QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Command", self.command)
        form.addRow("Arguments", self.args)
        form.addRow("Environment", self.env)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _check(self) -> None:
        if not NAME_RE.match(self.name.text().strip()) or not self.command.text().strip():
            QtWidgets.QMessageBox.warning(self, "MCP server", "Give a name (lower-case letters, digits, "
                                          "hyphens) and a command.")
            return
        self.accept()

    def result_config(self) -> tuple[str, dict]:
        cfg = {"command": self.command.text().strip(), "args": shlex.split(self.args.text(), posix=True)}
        env = dict(line.split("=", 1) for line in self.env.toPlainText().splitlines() if "=" in line)
        if env:
            cfg["env"] = {k.strip(): v.strip() for k, v in env.items()}
        return self.name.text().strip(), cfg


class ManagerDialog(QtWidgets.QDialog):
    changed = QtCore.Signal()  # files on disk changed: the session should reload plugins
    ask_agent = QtCore.Signal(str)  # a prompt to put in the chat input

    def __init__(self, packs: list[Path], user_root: Path, mcp_status: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Skills, commands, subagents and MCP servers")
        self.resize(820, 560)
        self.packs = packs
        self.user_root = user_root
        self.mcp_status = {s["name"]: s for s in mcp_status}
        self.dirty = False

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_items_tab(), "Skills, commands, subagents")
        tabs.addTab(self._build_mcp_tab(), "MCP servers")
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(close)
        self.refresh()

    # ----- data -----
    def plugins(self) -> list[Plugin]:
        out = [Plugin(p, editable=False) for p in self.packs if (p / ".claude-plugin").is_dir()]
        self.user_plugin()  # make sure the default user plugin exists
        out += [Plugin(p, editable=True) for p in sorted(self.user_root.iterdir())
                if (p / ".claude-plugin").is_dir()]
        return out

    def user_plugin(self) -> Plugin:
        path = self.user_root / "user"
        meta = path / ".claude-plugin" / "plugin.json"
        if not meta.exists():
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({"name": "user", "version": "0.1.0",
                                        "description": "User-created skills, agents and commands"}, indent=2),
                            encoding="utf-8")
        return Plugin(path, editable=True)

    # ----- items tab -----
    def _build_items_tab(self) -> QtWidgets.QWidget:
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Description"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 80)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_item())
        buttons = QtWidgets.QHBoxLayout()
        for label, slot in (("New skill…", lambda: self.new_item("skill")),
                            ("New command…", lambda: self.new_item("command")),
                            ("New subagent…", lambda: self.new_item("agent")),
                            ("Ask the agent to write one…", self.ask_agent_to_write),
                            ("Open / edit", self.edit_item),
                            ("Copy to my plugin", self.copy_item),
                            ("Delete", self.delete_item),
                            ("Show folder", self.show_folder)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(self.tree)
        lay.addLayout(buttons)
        return w

    def refresh(self) -> None:
        self.tree.clear()
        for plug in self.plugins():
            where = "your plugin" if plug.editable else "bundled pack, read-only"
            top = QtWidgets.QTreeWidgetItem([f"{plug.name}", "", f"{where} · {plug.path}"])
            top.setFirstColumnSpanned(False)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            for kind, name, desc, path in plug.items():
                it = QtWidgets.QTreeWidgetItem([f"{plug.name}:{name}", kind, desc])
                it.setData(0, QtCore.Qt.UserRole, (str(path), plug.editable, kind))
                it.setToolTip(2, desc)
                top.addChild(it)
            top.setExpanded(True)
        self._refresh_mcp()

    def _selected(self):
        it = self.tree.currentItem()
        data = it.data(0, QtCore.Qt.UserRole) if it else None
        return (Path(data[0]), data[1], data[2]) if data else (None, False, None)

    def new_item(self, kind: str) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, f"New {kind}", "Name (lower-case, hyphens):")
        name = name.strip().lower()
        if not ok or not name:
            return
        if not NAME_RE.match(name):
            QtWidgets.QMessageBox.warning(self, f"New {kind}", "Use lower-case letters, digits and hyphens.")
            return
        desc, ok = QtWidgets.QInputDialog.getText(
            self, f"New {kind}", "One-line description (the agent uses this to decide when to use it):")
        if not ok:
            return
        rel, template = TEMPLATES[kind]
        path = self.user_plugin().path / rel.format(name=name)
        if path.exists():
            QtWidgets.QMessageBox.warning(self, f"New {kind}", f"{path} already exists.")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(name=name, description=desc.strip() or f"TODO describe {name}",
                                        title=name.replace("-", " ").capitalize()), encoding="utf-8")
        self._changed()
        EditorDialog(path, True, self).exec()
        self.refresh()

    def ask_agent_to_write(self) -> None:
        kinds = ["skill", "command", "subagent"]
        kind, ok = QtWidgets.QInputDialog.getItem(self, "Ask the agent", "What should it write?", kinds, 0, False)
        if not ok:
            return
        what, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Ask the agent", f"Describe the {kind} you want:")
        if not ok or not what.strip():
            return
        folder = {"skill": "skills/<name>/SKILL.md", "command": "commands/<name>.md",
                  "subagent": "agents/<name>.md"}[kind]
        target = (self.user_plugin().path / folder).as_posix()
        self.ask_agent.emit(
            f"Write a new Claude Code {kind} and save it as {target} (choose a short lower-case hyphenated "
            f"name). Use YAML frontmatter with name and a description that says when to use it. "
            f"Here is what it should do:\n\n{what.strip()}")
        self.accept()

    def edit_item(self) -> None:
        path, editable, _ = self._selected()
        if path:
            if EditorDialog(path, editable, self).exec() and editable:
                self._changed()
                self.refresh()

    def copy_item(self) -> None:
        path, editable, kind = self._selected()
        if not path or editable:
            return
        src = path.parent if kind == "skill" else path
        folder = {"skill": "skills", "command": "commands", "agent": "agents"}[kind]
        dst = self.user_plugin().path / folder / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            QtWidgets.QMessageBox.warning(self, "Copy", f"{dst} already exists in your plugin.")
            return
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
        self._changed()
        self.refresh()

    def delete_item(self) -> None:
        path, editable, kind = self._selected()
        if not path or not editable:
            return
        target = path.parent if kind == "skill" else path
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete {target}?") != QtWidgets.QMessageBox.Yes:
            return
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        self._changed()
        self.refresh()

    def show_folder(self) -> None:
        path, _, _ = self._selected()
        folder = path.parent if path else self.user_plugin().path
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    # ----- MCP tab -----
    def _build_mcp_tab(self) -> QtWidgets.QWidget:
        self.mcp_tree = QtWidgets.QTreeWidget()
        self.mcp_tree.setHeaderLabels(["Server", "Status", "Plugin", "Command"])
        self.mcp_tree.setColumnWidth(0, 160)
        self.mcp_tree.setColumnWidth(1, 150)
        buttons = QtWidgets.QHBoxLayout()
        for label, slot in (("Add server…", self.add_mcp), ("Edit…", self.edit_mcp),
                            ("Remove", self.remove_mcp)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        note = QtWidgets.QLabel("Servers in bundled packs are read-only. Changes apply when the chat reloads.")
        note.setStyleSheet("color: gray;")
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(self.mcp_tree)
        lay.addWidget(note)
        lay.addLayout(buttons)
        return w

    def _refresh_mcp(self) -> None:
        self.mcp_tree.clear()
        for plug in self.plugins():
            for name, cfg in plug.mcp_servers().items():
                live = self.mcp_status.get(f"plugin:{plug.name}:{name}", {})
                status = live.get("status", "not loaded yet")
                if live.get("status") == "connected":
                    status = f"connected · {live.get('tools', 0)} tools"
                cmd = " ".join([cfg.get("command", cfg.get("url", ""))] + cfg.get("args", []))
                it = QtWidgets.QTreeWidgetItem([name, status, plug.name, cmd])
                it.setData(0, QtCore.Qt.UserRole, (str(plug.path), plug.editable, name))
                it.setToolTip(3, cmd)
                self.mcp_tree.addTopLevelItem(it)

    def _selected_mcp(self):
        it = self.mcp_tree.currentItem()
        if not it:
            return None, None
        path, editable, name = it.data(0, QtCore.Qt.UserRole)
        return (Plugin(Path(path), editable), name) if editable else (None, name)

    def add_mcp(self) -> None:
        dlg = McpDialog(parent=self)
        if dlg.exec():
            name, cfg = dlg.result_config()
            plug = self.user_plugin()
            servers = plug.mcp_servers()
            servers[name] = cfg
            plug.save_mcp_servers(servers)
            self._changed()
            self.refresh()

    def edit_mcp(self) -> None:
        plug, name = self._selected_mcp()
        if plug is None:
            if name:
                QtWidgets.QMessageBox.information(self, "MCP server", "This server belongs to a bundled pack.")
            return
        servers = plug.mcp_servers()
        dlg = McpDialog(name, servers.get(name), self)
        if dlg.exec():
            new_name, cfg = dlg.result_config()
            servers.pop(name, None)
            servers[new_name] = cfg
            plug.save_mcp_servers(servers)
            self._changed()
            self.refresh()

    def remove_mcp(self) -> None:
        plug, name = self._selected_mcp()
        if plug is None or QtWidgets.QMessageBox.question(self, "Remove", f"Remove MCP server {name}?") \
                != QtWidgets.QMessageBox.Yes:
            return
        servers = plug.mcp_servers()
        servers.pop(name, None)
        plug.save_mcp_servers(servers)
        self._changed()
        self.refresh()

    def _changed(self) -> None:
        self.dirty = True
        self.changed.emit()
