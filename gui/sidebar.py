"""Collapsible sidebar of past chats, like the Claude desktop app's.

Saved chats (Markdown files from Save) sit in their own group at the top; past chats are
grouped by date and open in a new tab, resumed with their full context.
"""

from __future__ import annotations

import datetime
import os

from .qt import QtCore, QtGui, QtWidgets

ROLE = QtCore.Qt.UserRole


class ChatSidebar(QtWidgets.QWidget):
    open_session = QtCore.Signal(str, str)   # backend_session_id, cwd
    attach_saved = QtCore.Signal(str)        # path of a saved chat (.md)
    save_session = QtCore.Signal(str, str)   # backend_session_id, title
    delete_session = QtCore.Signal(str)      # backend_session_id
    refresh_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sessions: list[dict] = []
        self.saved: list[dict] = []
        self.open_ids: set[str] = set()

        title = QtWidgets.QLabel("<b>Chats</b>")
        refresh = QtWidgets.QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Refresh")
        refresh.setAutoRaise(True)
        refresh.clicked.connect(self.refresh_requested.emit)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(title, 1)
        head.addWidget(refresh)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search chats…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(10)
        self.tree.setTextElideMode(QtCore.Qt.ElideRight)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemClicked.connect(self._activate)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(head)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        self.setMinimumWidth(170)

    # ---------- data ----------
    def set_sessions(self, items: list[dict]) -> None:
        self.sessions = items
        self._fill()

    def set_saved(self, items: list[dict]) -> None:
        self.saved = items
        self._fill()

    def set_open(self, ids: set[str]) -> None:
        self.open_ids = {i for i in ids if i}
        self._fill()

    # ---------- view ----------
    def _fill(self) -> None:
        q = self.search.text().strip().lower()
        expanded = {self.tree.topLevelItem(i).text(0).split("  ")[0]: self.tree.topLevelItem(i).isExpanded()
                    for i in range(self.tree.topLevelItemCount())}
        self.tree.clear()

        saved = [s for s in self.saved if not q or q in (s["title"] + " " + s.get("note", "")).lower()]
        if saved:
            head = self._group("Saved", len(saved), expanded)
            for s in saved:
                it = QtWidgets.QTreeWidgetItem([f"\U0001f4be {s['title']}"])
                it.setToolTip(0, f"{s['title']}\n{s['saved']} · {s['model']}\n{s.get('note', '')}\n\n"
                                 "Click: attach to the current chat as context")
                it.setData(0, ROLE, ("saved", s["path"]))
                head.addChild(it)

        today = datetime.date.today()
        groups: dict[str, list[dict]] = {}
        for s in sorted(self.sessions, key=lambda s: s.get("last_modified") or 0, reverse=True):
            text = (s.get("summary") or "(untitled)").replace("\n", " ")
            if q and q not in (text + " " + (s.get("cwd") or "")).lower():
                continue
            day = datetime.datetime.fromtimestamp((s.get("last_modified") or 0) / 1000).date()
            age = (today - day).days
            label = ("Today" if age <= 0 else "Yesterday" if age == 1 else
                     "Previous 7 days" if age <= 7 else "Previous 30 days" if age <= 30 else "Older")
            groups.setdefault(label, []).append(s)
        for label in ("Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"):
            items = groups.get(label)
            if not items:
                continue
            head = self._group(label, len(items), expanded)
            for s in items:
                text = (s.get("summary") or "(untitled)").replace("\n", " ")
                mark = "● " if s["backend_session_id"] in self.open_ids else ""
                it = QtWidgets.QTreeWidgetItem([mark + text])
                when = datetime.datetime.fromtimestamp((s.get("last_modified") or 0) / 1000)
                it.setToolTip(0, f"{text}\n{when:%d %b %Y %H:%M}\n{s.get('cwd') or ''}\n\n"
                                 "Click: open in a new tab (continues with full context)")
                it.setData(0, ROLE, ("session", s["backend_session_id"], s.get("cwd") or "", text))
                head.addChild(it)
        if not self.tree.topLevelItemCount():
            self.tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["No chats yet" if not q else "No matches"]))

    def _group(self, label: str, n: int, expanded: dict) -> QtWidgets.QTreeWidgetItem:
        head = QtWidgets.QTreeWidgetItem([f"{label}  ({n})"])
        font = head.font(0)
        font.setBold(True)
        head.setFont(0, font)
        head.setForeground(0, QtGui.QBrush(QtGui.QColor("#888")))
        head.setFlags(head.flags() & ~QtCore.Qt.ItemIsSelectable)
        self.tree.addTopLevelItem(head)
        head.setExpanded(expanded.get(label, label in ("Saved", "Today", "Yesterday", "Previous 7 days")))
        return head

    # ---------- actions ----------
    def _activate(self, item, _col=0) -> None:
        data = item.data(0, ROLE) if item else None
        if not data:
            if item is not None and item.childCount():
                item.setExpanded(not item.isExpanded())
            return
        if data[0] == "session":
            self.open_session.emit(data[1], data[2])
        else:
            self.attach_saved.emit(data[1])

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        data = item.data(0, ROLE) if item else None
        if not data:
            return
        menu = QtWidgets.QMenu(self)
        if data[0] == "session":
            menu.addAction("Open in a new tab", lambda: self.open_session.emit(data[1], data[2]))
            menu.addAction("Save as file (to load as context later)…",
                           lambda: self.save_session.emit(data[1], data[3][:60]))
            menu.addSeparator()
            menu.addAction("Delete (moves to runtime/trash)…", lambda: self._confirm_delete(data[1], data[3]))
        else:
            menu.addAction("Attach to the current chat as context", lambda: self.attach_saved.emit(data[1]))
            menu.addAction("Open the file", lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(data[1])))
            menu.addAction("Show in folder", lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(os.path.dirname(data[1]))))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _confirm_delete(self, backend_id: str, text: str) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Delete chat", f"Delete this chat?\n\n{text[:120]}\n\n"
                "It is moved to runtime/trash and can be recovered from there.") == QtWidgets.QMessageBox.Yes:
            self.delete_session.emit(backend_id)
