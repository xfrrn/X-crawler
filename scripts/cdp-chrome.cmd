@echo off
setlocal EnableDelayedExpansion
REM Launch Chrome/Edge with CDP remote debugging for MediaCrawler.
REM Usage:  scripts\cdp-chrome.cmd
REM First run: log in douyin / kuaishou / xiaohongshu in the window;
REM the login persists in the profile. Keep the window open before crawls.
REM See README "Multi-platform monitoring" -> "CDP browser startup" section.
REM (Windows cmd code page quirks: keep this file ASCII-only. Paths like
REM "Program Files (x86)" contain parens, so use !VAR! inside if-blocks,
REM NOT %VAR%, or the block parsing breaks.)

set "PROFILE=%~dp0..\browser_data\cdp"
if not exist "%PROFILE%" mkdir "%PROFILE%"

REM find browser: Chrome wins over Edge. Check Edge first, then Chrome
REM (later "if exist" overwrites CHROME, so Chrome ends up preferred).
set "CHROME="
set "P4=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "P5=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "P1=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "P2=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "P3=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if exist "%P4%" set "CHROME=%P4%"
if exist "%P5%" set "CHROME=%P5%"
if exist "%P1%" set "CHROME=%P1%"
if exist "%P2%" set "CHROME=%P2%"
if exist "%P3%" set "CHROME=%P3%"
if not defined CHROME (
    echo [ERROR] Chrome/Edge not found. Edit this script or use the command in README.
    pause
    exit /b 1
)

REM port 9222 in use = debug browser already running, just reuse it
netstat -ano | findstr ":9222" | findstr "LISTEN" >nul
if !errorlevel!==0 (
    echo [INFO] 9222 already listening - a debug browser is already running.
    echo        Use that window directly. Browser: !CHROME!
    exit /b 0
)

echo Launching browser (CDP port 9222, profile=%PROFILE%)
echo Browser: %CHROME%
echo Log in douyin / kuaishou / xiaohongshu in the new window, then keep it open.
REM --no-proxy-server: this browser only visits CN platforms, connect directly
REM (independent of any system proxy / Clash). Douyin/Kuaishou/XHS work fine direct.
start "" "%CHROME%" --no-proxy-server --remote-debugging-port=9222 --user-data-dir="%PROFILE%" https://www.douyin.com

REM wait a few seconds and confirm 9222 actually came up
set /a WAIT=0
:CHECK
ping -n 2 127.0.0.1 >nul
set /a WAIT+=1
netstat -ano | findstr ":9222" | findstr "LISTEN" >nul
if !errorlevel!==0 goto UP
if %WAIT% LSS 6 goto CHECK

echo.
echo [WARN] 9222 not listening after a few seconds. Most likely a normal Chrome is
echo        already running and the new instance merged into it without the debug port.
echo        Quit ALL Chrome first, then re-run this script.
pause
exit /b 1

:UP
echo.
echo [OK] 9222 listening, CDP browser ready. Start the service to crawl via CDP.
exit /b 0
