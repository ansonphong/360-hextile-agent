@echo off
REM Optional Windows launcher for committed .mcp.json (which stays python3).
set "SCRIPT=%~dp0hextile_mcp.py"
where python >nul 2>&1 && python "%SCRIPT%" %* && exit /b %ERRORLEVEL%
where python3 >nul 2>&1 && python3 "%SCRIPT%" %* && exit /b %ERRORLEVEL%
py -3 "%SCRIPT%" %*
