# Start PA in the background (tray icon near the clock) and open the My Day page.
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
Start-Process -FilePath $pyw -ArgumentList "-m", "pa", "supervise" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:8765/"
