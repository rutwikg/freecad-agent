"""Qt binding shim: hosts ship PySide6 (FreeCAD 1.1) or PySide2 (FreeCAD 1.0 and older hosts).
Kept dependency-free so it runs inside a host's own Python without qtpy.

The rest of the GUI is written against the Qt6 API; the few Qt5 differences are
bridged here, once:
- QShortcut / QAction live in QtGui in Qt6 but in QtWidgets in Qt5.
- PySide2 names the modal loop exec_(); Qt6 code calls exec().
"""

try:
    from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    from PySide6.QtGui import QAction, QShortcut  # noqa: F401
    QT6 = True
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets  # noqa: F401
    from PySide2.QtWidgets import QAction, QShortcut  # noqa: F401
    QT6 = False
    for _cls in (QtWidgets.QDialog, QtWidgets.QMenu, QtCore.QCoreApplication, QtCore.QEventLoop):
        if not hasattr(_cls, "exec"):
            _cls.exec = _cls.exec_
