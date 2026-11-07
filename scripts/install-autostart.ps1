# Start PA automatically when you log in to Windows (no admin needed).
# It puts a shortcut in your Startup folder. Remove it with:  -Remove
param([switch]$Remove)
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "PA personal assistant.lnk"
if ($Remove) {
    Remove-Item $lnk -ErrorAction SilentlyContinue
    Write-Host "PA will no longer start at login."
    exit 0
}
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
$s.Arguments = "-m pa supervise"
$s.WorkingDirectory = $root
$s.Description = "PA - listens and makes notes"
$s.Save()
Write-Host "PA will start when you log in. Shortcut: $lnk"
