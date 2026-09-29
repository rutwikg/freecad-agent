"""Offscreen GUI test of the chat panel against the real engine, without ever sending a
prompt to a model: tabs with independent sessions, thinking levels, command palette,
markdown streaming (simulated events), attachments, history rendering, manager dialog.
Saves screenshots to runtime/gui_test_*.png.

    set QT_QPA_PLATFORM=offscreen & set QT_QPA_FONTDIR=C:/Windows/Fonts
    uv run python tools/gui_offscreen_test.py [backend_session_id_to_resume]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.qt import QtCore, QtGui, QtWidgets  # noqa: E402
from gui.chat_widget import ChatPanel, ROOT, CONFIG  # noqa: E402
from gui.manager import ManagerDialog  # noqa: E402

RESUME = sys.argv[1] if len(sys.argv) > 1 else None
OUT = ROOT / "runtime"
app = QtWidgets.QApplication(sys.argv)


def fake_capture(path):
    img = QtGui.QImage(320, 200, QtGui.QImage.Format_RGB32)
    img.fill(QtGui.QColor("#446"))
    img.save(path)


panel = ChatPanel(host_name="offscreen", host_capture=fake_capture)
panel.resize(560, 860)
panel.show()
steps = []


def step(delay_ms):
    def deco(fn):
        steps.append((delay_ms, fn))
        return fn
    return deco


def tab(i=None):
    return panel.tabs.currentWidget() if i is None else panel.tabs.widget(i)


def snap(name):
    panel.grab().save(str(OUT / f"gui_test_{name}.png"))
    print("saved", name)


def ev(t, **m):
    t.on_event({"session": t.session, **m})


@step(15000)
def first_tab():
    t = tab()
    print("tab1 session:", t.session, "ready:", t.session_ready, "| thinking levels:", t.thinking_levels,
          "current:", t.current_effort(), "| session effort:", t.session_effort)
    t.input.setPlainText("/fr")
    print("palette items:", [t.palette.item(i).text()[:32] for i in range(t.palette.count())])
    t.input.clear()


@step(500)
def markdown():
    t = tab()
    t.panel.set_title(t, "Summarise the bracket part please")
    t._append("<p><b>You:</b> Summarise the part</p>")
    t._busy(True)
    t.activity.start("waiting for model")
    for chunk in ["## Bracket summary\n\n", "The part has **three** features:\n\n",
                  "1. A 40 mm base pad\n2. Two M6 holes\n3. A fillet\n\n",
                  "```python\nbox = doc.addObject('Part::Box', 'Base')\n```\n"]:
        ev(t, type="text.delta", text=chunk)
    print("tab title while busy:", repr(panel.tabs.tabText(0)))
    QtCore.QTimer.singleShot(300, lambda: ev(t, type="turn.end", status="success", usage={}, turns=1,
                                             duration_ms=900))


@step(1500)
def second_tab():
    first = tab(0)
    second = panel.new_tab()
    print("tabs:", panel.tabs.count(), [panel.tabs.tabText(i) for i in range(panel.tabs.count())])
    print("tab2 transcript empty:", not second.transcript.toPlainText().strip(),
          "| tab1 transcript kept:", "Bracket summary" in first.transcript.toPlainText())


@step(15000)
def second_tab_ready():
    a, b = tab(0), tab(1)
    print("independent sessions:", a.session, b.session, a.session != b.session, "| tab2 ready:", b.session_ready)
    snap("tabs")


@step(500)
def thinking_change():
    b = tab(1)
    levels = [b.think.itemData(i) for i in range(b.think.count())]
    target = "off" if "off" in levels else levels[0]
    old = b.session
    b.think.setCurrentIndex(b.think.findData(target))
    print(f"thinking -> {target}: session restarted {old} -> {b.session}, effort sent: {b.session_effort}")


@step(10000)
def close_second():
    panel.close_tab(1)
    print("after closing tab 2:", panel.tabs.count(), "tab(s)")


@step(500)
def history():
    if RESUME:
        tab(0).resume(RESUME)


@step(12000)
def after_history():
    if RESUME:
        print("resumed transcript chars:", len(tab(0).transcript.toPlainText()))
        snap("history")


@step(500)
def finish():
    panel.shutdown()
    app.quit()


t = 0
for delay, fn in steps:
    t += delay
    QtCore.QTimer.singleShot(t, fn)
app.exec()
