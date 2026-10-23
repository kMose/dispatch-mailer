@echo off
rem Start Dispatch with this folder's campaign.yaml, whatever the working directory is.
cd /d "%~dp0"
if exist "%~dp0dist\Dispatch\Dispatch.exe" (
    start "" "%~dp0dist\Dispatch\Dispatch.exe" "%~dp0campaign.yaml"
) else (
    start "" "%~dp0.venv\Scripts\dispatch-gui.exe" "%~dp0campaign.yaml"
)
