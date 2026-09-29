# FreeCAD Agent addon. Loaded either from FreeCAD's user Mod folder (a folder link to this
# directory) or with:  freecad.exe -M <package>\hosts\freecad\FreeCADAgent
# All logic lives in freecad_agent_dock.py (see its docstring for why).
import FreeCADGui

# Both load paths can be active at once (Mod link + run_freecad.bat); set up only once.
if not getattr(FreeCADGui, "_freecad_agent_loaded", False):
    FreeCADGui._freecad_agent_loaded = True
    from PySide import QtCore

    import freecad_agent_dock

    FreeCADGui.addCommand("Agent_Chat", freecad_agent_dock.AgentChatCommand())
    FreeCADGui.addWorkbenchManipulator(freecad_agent_dock.AgentManipulator())
    QtCore.QTimer.singleShot(1500, freecad_agent_dock.startup)
