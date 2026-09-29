"""Slash-command palette: a filtered list above the input while the user types "/…".

Up/Down move, Tab or Enter completes, Esc closes. Entries come from the engine's
session.info (pack and user commands and skills, useful built-ins) plus the GUI's
own local commands.
"""

from __future__ import annotations

import html

from .qt import QtCore, QtWidgets

# Built-in CLI commands that work headless and make sense in a chat panel.
# The rest (config, permissions, mcp, color, …) are terminal dialogs.
USEFUL_BUILTINS = {"compact", "context", "init", "review", "security-review", "code-review",
                   "simplify", "debug", "usage", "recap"}


class CommandPalette(QtWidgets.QListWidget):
    chosen = QtCore.Signal(str)  # command name without the slash

    def __init__(self, editor: QtWidgets.QPlainTextEdit, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.commands: list[dict] = []
        self.setMaximumHeight(170)
        self.setWordWrap(True)
        self.setStyleSheet("QListWidget { font-size: 12px; }")
        self.itemClicked.connect(lambda it: self._choose(it))
        editor.textChanged.connect(self._refilter)
        editor.installEventFilter(self)
        self.hide()

    def set_commands(self, engine_commands: list[dict], local_commands: list[dict]) -> None:
        cmds = [c for c in engine_commands if not c.get("builtin") or c["name"] in USEFUL_BUILTINS]
        # Pack and user commands first, then local GUI commands, then built-ins.
        cmds.sort(key=lambda c: (bool(c.get("builtin")), c["name"]))
        self.commands = [c for c in cmds if not c.get("builtin")] + local_commands + \
                        [c for c in cmds if c.get("builtin")]

    def _query(self) -> str | None:
        text = self.editor.toPlainText()
        if not text.startswith("/") or " " in text or "\n" in text:
            return None
        return text[1:].lower()

    def _refilter(self) -> None:
        q = self._query()
        if q is None or not self.commands:
            self.hide()
            return
        self.clear()
        starts = [c for c in self.commands if c["name"].lower().startswith(q)]
        contains = [c for c in self.commands if q in c["name"].lower() and c not in starts]
        for c in starts + contains:
            hint = f" {c['argument_hint']}" if c.get("argument_hint") else ""
            item = QtWidgets.QListWidgetItem(f"/{c['name']}{hint}  —  {c.get('description', '')[:110]}")
            item.setData(QtCore.Qt.UserRole, c["name"])
            item.setToolTip(html.escape(c.get("description", "")))
            self.addItem(item)
        if self.count():
            self.setCurrentRow(0)
            self.show()
        else:
            self.hide()

    def _choose(self, item) -> None:
        if item is None:
            return
        name = item.data(QtCore.Qt.UserRole)
        self.hide()
        self.editor.setPlainText(f"/{name} ")
        cur = self.editor.textCursor()
        cur.movePosition(cur.MoveOperation.End if hasattr(cur, "MoveOperation") else cur.End)
        self.editor.setTextCursor(cur)
        self.chosen.emit(name)

    def eventFilter(self, obj, event):
        if obj is self.editor and event.type() == QtCore.QEvent.KeyPress and self.isVisible():
            key = event.key()
            if key in (QtCore.Qt.Key_Down, QtCore.Qt.Key_Up):
                step = 1 if key == QtCore.Qt.Key_Down else -1
                self.setCurrentRow((self.currentRow() + step) % self.count())
                return True
            if key in (QtCore.Qt.Key_Tab, QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) \
                    and not event.modifiers() & QtCore.Qt.ControlModifier:
                self._choose(self.currentItem())
                return True
            if key == QtCore.Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, event)
