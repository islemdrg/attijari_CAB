$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Red
Write-Host " Attijari Bank - CAB Quality Dashboard" -ForegroundColor White
Write-Host "=========================================" -ForegroundColor Red
Write-Host ""

& ".\env\Scripts\python.exe" ".\web_app.py"