@echo off
REM Double-click on Windows. Localhost setup wizard only.
cd /d "%~dp0\.."
echo GalleryEdge setup wizard
echo ========================
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -e ".[panel]"
echo.
echo Starting localhost wizard...
ge-panel --setup --setup-dir "%USERPROFILE%\galleryedge-data"
pause
