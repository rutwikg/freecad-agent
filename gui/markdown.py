"""Markdown -> HTML with Qt's own GitHub-flavoured parser, so hosts need no extra package."""

from __future__ import annotations

import re

from .qt import QtGui

_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.S)
# Qt emits the generic family 'monospace', which Windows does not map to a fixed-width font.
_MONO = "font-family:'Consolas','Cascadia Mono','Courier New',monospace;"


def md_to_html(text: str) -> str:
    doc = QtGui.QTextDocument()
    doc.setMarkdown(text, QtGui.QTextDocument.MarkdownDialectGitHub)
    m = _BODY.search(doc.toHtml())
    out = m.group(1) if m else doc.toHtml()
    out = out.replace("font-family:'monospace';", _MONO)
    # Indent code blocks; no background colour, so it reads in light and dark host themes.
    return out.replace("<pre style=\" margin-top:0px; margin-bottom:0px; margin-left:0px;",
                       "<pre style=\" margin-top:4px; margin-bottom:4px; margin-left:14px;")
