@echo off
rem Abre o painel de licencas (interface grafica). Requer: py -3.12 -m pip install -r requirements.txt
cd /d "%~dp0"
start "" pyw -3.12 -m admin.painel %*
