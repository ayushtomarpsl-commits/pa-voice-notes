# One-time setup for PA. Run in Windows PowerShell:
#   powershell -ExecutionPolicy Bypass -File "D:\Rnd\PA\scripts\setup.ps1"
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating Python 3.14 environment..."
    py -3.14 -m venv (Join-Path $root ".venv")
}
Write-Host "Installing packages (a few minutes the first time)..."
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r (Join-Path $root "requirements.txt")

Write-Host "Downloading the speech model (about 480 MB, one time)..."
& $py -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

Write-Host "Downloading the voice recognition model (about 30 MB, one time)..."
$voiceDir = Join-Path $root "models\speaker"
New-Item -ItemType Directory -Force $voiceDir | Out-Null
$voiceModel = Join-Path $voiceDir "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
if (-not (Test-Path $voiceModel)) {
    curl.exe -sL -o $voiceModel "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
}

Write-Host "Copying Claude Code skills to $env:USERPROFILE\.claude\skills ..."
foreach ($name in @("pa-today", "pa-process")) {
    $dst = Join-Path $env:USERPROFILE ".claude\skills\$name"
    New-Item -ItemType Directory -Force $dst | Out-Null
    Copy-Item (Join-Path $root "claude-skills\$name\SKILL.md") $dst -Force
}

Write-Host "Running tests..."
& $py -m pytest -q (Join-Path $root "tests")

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Start PA:            powershell -File `"$root\scripts\start.ps1`""
Write-Host "  2. Start at login:      powershell -File `"$root\scripts\install-autostart.ps1`""
Write-Host "  3. Connect Claude Code: powershell -File `"$root\scripts\register-mcp.ps1`""
