@echo off
rem Cheats For Everything: local web UI + agent on LM Studio (no cloud, no cost)
cd /d "%~dp0"
rem VS Code / Electron terminals export ELECTRON_RUN_AS_NODE=1: LM Studio (Electron) then starts as bare
rem node and exits at once, and "lms server start" times out waiting for the daemon.
set ELECTRON_RUN_AS_NODE=
python -c "import pymem, psutil, numpy, PIL, dnfile, PyInstaller" 2>nul || pip install -r requirements.txt
rem start LM Studio and load the best installed model, GPU share sized from free video memory
python -c "from trainerkit import lmstudio; print('model:', lmstudio.ensure())"
python app\server.py
pause
