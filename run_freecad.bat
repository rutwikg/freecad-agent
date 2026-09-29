@echo off
rem Start FreeCAD with the agent addon loaded from this folder (nothing installed in AppData).
rem Uses %FREECAD_EXE% if set, otherwise the first of FreeCAD 1.1 / 1.0 found in Program Files.
rem The FreeCAD MCP addon (FreeCADMCP) must be installed in FreeCAD itself.
setlocal
set PKG=%~dp0
if not defined FREECAD_EXE (
  for %%V in ("FreeCAD 1.1" "FreeCAD 1.0") do (
    if not defined FREECAD_EXE if exist "%ProgramFiles%\%%~V\bin\freecad.exe" set "FREECAD_EXE=%ProgramFiles%\%%~V\bin\freecad.exe"
  )
)
if not defined FREECAD_EXE (
  echo FreeCAD 1.0 or 1.1 not found. Set FREECAD_EXE to the full path of freecad.exe.
  pause
  exit /b 1
)
start "" "%FREECAD_EXE%" -M "%PKG%hosts\freecad\FreeCADAgent" %*
