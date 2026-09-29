"""Docks the host-independent chat widget into FreeCAD's main window.

Lives in its own module because FreeCAD exec()s InitGui.py in a throwaway
namespace: functions defined there cannot see each other when called later.
"""


def addon_dir():
    """This addon's real folder. realpath() matters: the addon may be loaded through a
    folder link in FreeCAD's user Mod folder, and the package root must be found from
    the link's target, not from AppData."""
    import os
    return os.path.dirname(os.path.realpath(__file__))


def package_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.dirname(addon_dir())))


class AgentManipulator:
    """Keeps the Agent entry in the View menu and toolbar across workbench switches
    (FreeCAD rebuilds menus on every switch, so plain QMenu edits would vanish)."""

    def modifyMenuBar(self):
        return [{"insert": "Agent_Chat", "menuItem": "Std_DockViewMenu", "after": ""}]

    def modifyToolBars(self):
        return [{"append": "Agent_Chat", "toolBar": "View"}]


def _free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def claim_private_rpc_port():
    """Move this FreeCAD's MCP RPC server off the shared 9875 onto a private port.

    FreeCAD's XML-RPC server binds with SO_REUSEADDR, so on Windows every open
    FreeCAD can listen on 9875 at once and MCP calls reach whichever one Windows
    picks. Returns the private port, or None if the FreeCADMCP addon is missing.
    """
    import FreeCAD
    try:
        from rpc_server import rpc_server
    except ImportError:
        FreeCAD.Console.PrintWarning("FreeCAD Agent: FreeCADMCP addon not found; FreeCAD tools unavailable\n")
        return None
    rpc_server.stop_rpc_server()
    port = _free_port()
    msg = rpc_server.start_rpc_server(port=port)
    FreeCAD.Console.PrintMessage("FreeCAD Agent: %s\n" % msg)
    return port if str(port) in msg else None


def capture_view(path):
    """Save the active 3D view as an image for the chat (vision models can read it)."""
    import FreeCADGui
    gui_doc = FreeCADGui.ActiveDocument
    if gui_doc is None:
        raise RuntimeError("no document is open")
    view = gui_doc.ActiveView
    if not hasattr(view, "saveImage"):
        raise RuntimeError("the active window is not a 3D view")
    view.saveImage(path, 1280, 800, "Current")


def show_dock():
    import sys
    import FreeCAD
    import FreeCADGui
    from PySide import QtCore, QtWidgets

    mw = FreeCADGui.getMainWindow()
    dock = mw.findChild(QtWidgets.QDockWidget, "FreeCADAgentDock")
    if dock:
        dock.show()
        dock.raise_()
        return
    root = package_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from gui.chat_widget import ChatPanel

    port = claim_private_rpc_port()
    host_env = {"FREECAD_RPC_PORT": str(port)} if port else {}
    host_name = "FreeCAD " + ".".join(FreeCAD.Version()[:3])
    if port:
        host_name += " · RPC port %d" % port
    widget = ChatPanel(host_name=host_name, host_env=host_env, host_capture=capture_view)
    dock = QtWidgets.QDockWidget("Agent", mw)
    dock.setObjectName("FreeCADAgentDock")
    dock.setWidget(widget)
    mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
    QtWidgets.QApplication.instance().aboutToQuit.connect(widget.shutdown)
    FreeCAD.Console.PrintMessage("FreeCAD Agent: chat dock ready\n")


class AgentChatCommand:
    def GetResources(self):
        import os
        return {"MenuText": "Agent chat", "ToolTip": "Show or hide the agent chat panel",
                "Pixmap": os.path.join(addon_dir(), "resources", "agent.svg")}

    def Activated(self):
        import FreeCADGui
        from PySide import QtWidgets
        dock = FreeCADGui.getMainWindow().findChild(QtWidgets.QDockWidget, "FreeCADAgentDock")
        if dock is not None and dock.isVisible():
            dock.hide()
        else:
            show_dock()

    def IsActive(self):
        return True


def startup():
    import FreeCAD
    try:
        show_dock()
    except Exception as e:
        import traceback
        FreeCAD.Console.PrintError("FreeCAD Agent failed to start: %s\n%s\n" % (e, traceback.format_exc()))
