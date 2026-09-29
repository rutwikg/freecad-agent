"""Past-conversation picker for resuming a chat."""

from __future__ import annotations

import datetime

from .qt import QtCore, QtWidgets


class HistoryDialog(QtWidgets.QDialog):
    def __init__(self, items: list[dict], cwd: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chat history")
        self.resize(560, 420)
        self.chosen: str | None = None

        self.only_here = QtWidgets.QCheckBox("Only chats in this folder")
        self.only_here.setChecked(True)
        self.only_here.toggled.connect(self._fill)
        self.list = QtWidgets.QListWidget()
        self.list.itemDoubleClicked.connect(self._accept_item)
        open_btn = QtWidgets.QPushButton("Resume")
        open_btn.setDefault(True)
        open_btn.clicked.connect(lambda: self._accept_item(self.list.currentItem()))
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(open_btn)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.only_here)
        lay.addWidget(self.list, 1)
        lay.addLayout(buttons)

        self.items = sorted(items, key=lambda i: i.get("last_modified") or 0, reverse=True)
        self.cwd = _norm(cwd)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for it in self.items:
            if self.only_here.isChecked() and _norm(it.get("cwd") or "") != self.cwd:
                continue
            when = datetime.datetime.fromtimestamp((it.get("last_modified") or 0) / 1000)
            summary = (it.get("summary") or "(untitled)").replace("\n", " ")
            row = QtWidgets.QListWidgetItem(f"{when:%d %b %H:%M}   {summary[:120]}")
            row.setToolTip(f"{summary}\n\n{it.get('cwd')}")
            row.setData(QtCore.Qt.UserRole, it["backend_session_id"])
            self.list.addItem(row)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _accept_item(self, item) -> None:
        if item is not None:
            self.chosen = item.data(QtCore.Qt.UserRole)
            self.accept()


class LoadContextDialog(QtWidgets.QDialog):
    """Pick saved chats (runtime/saved_sessions/*.md) to attach to the next message of a new
    chat as context. Works across models and backends, unlike Resume."""

    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        from .markdown import md_to_html
        self._md = md_to_html
        self.setWindowTitle("Load saved chats as context")
        self.resize(820, 520)
        self.paths: list[str] = []

        self.list = QtWidgets.QListWidget()
        for it in items:
            label = f"{it['title']}\n{it['saved']} · {it['model']} · {it['size_kb']} KB"
            if it.get("note"):
                label += f"\n{it['note']}"
            row = QtWidgets.QListWidgetItem(label)
            row.setFlags(row.flags() | QtCore.Qt.ItemIsUserCheckable)
            row.setCheckState(QtCore.Qt.Unchecked)
            row.setData(QtCore.Qt.UserRole, it["path"])
            self.list.addItem(row)
        self.list.currentItemChanged.connect(self._preview)
        self.list.itemChanged.connect(lambda _it: self._update_button())
        self.preview = QtWidgets.QTextBrowser()

        split = QtWidgets.QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.preview)
        split.setSizes([300, 520])
        note = QtWidgets.QLabel("Tick one or more chats. They are attached to your next message; long ones are "
                                "shortened to fit the model's context, and the agent can read the rest from the file.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Attach to next message", QtWidgets.QDialogButtonBox.AcceptRole)
        self.ok.setEnabled(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(note)
        lay.addWidget(buttons)
        if items:
            self.list.setCurrentRow(0)
        else:
            self.preview.setPlainText("No saved chats yet. Use Save (or /save) in a chat first.")

    def _checked(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.Checked]

    def _update_button(self) -> None:
        n = len(self._checked())
        self.ok.setEnabled(n > 0)
        self.ok.setText(f"Attach {n} chat{'s' if n != 1 else ''} to next message" if n else "Attach to next message")

    def _preview(self, item) -> None:
        if item is None:
            return
        try:
            text = open(item.data(QtCore.Qt.UserRole), encoding="utf-8").read()
        except OSError as e:
            text = f"could not read: {e}"
        self.preview.setHtml(self._md(text[:60000]))

    def _accept(self) -> None:
        self.paths = self._checked()
        if self.paths:
            self.accept()


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()
