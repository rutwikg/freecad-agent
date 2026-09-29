"""Starts the freecad-mcp server pointed at one specific FreeCAD instance.

freecad-mcp is by neka-nat (https://github.com/neka-nat/freecad-mcp, MIT licence); this
launcher uses it unmodified and only changes which RPC port it connects to.

freecad-mcp hard-codes RPC port 9875, and FreeCAD's XML-RPC server binds with
SO_REUSEADDR, so on Windows several FreeCAD windows can all listen on 9875 and
MCP calls land in whichever one Windows picks. The FreeCAD Agent dock gives its
own FreeCAD a private port and passes it here as FREECAD_RPC_PORT.
"""

import os

import freecad_mcp.server as server

PORT = int(os.environ.get("FREECAD_RPC_PORT") or 9875)
_Connection = server.FreeCADConnection


class _PinnedConnection(_Connection):
    def __init__(self, *args, **kwargs):
        kwargs["port"] = PORT
        super().__init__(*args, **kwargs)


server.FreeCADConnection = _PinnedConnection

if __name__ == "__main__":
    server.main()
