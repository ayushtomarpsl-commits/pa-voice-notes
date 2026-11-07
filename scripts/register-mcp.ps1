# Connect PA to Claude Code for ALL your projects (user scope), so in any folder
# (any project folder) Claude can read today's notes and tick off to-dos.
# It adds "pa-notes" to %USERPROFILE%\.claude.json and keeps a backup next to it.
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$code = @"
import json, shutil, sys
from pathlib import Path
p = Path.home() / '.claude.json'
data = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
if p.exists():
    shutil.copy2(p, p.with_name('.claude.json.bak-pa'))
data.setdefault('mcpServers', {})['pa-notes'] = {
    'type': 'stdio', 'command': sys.argv[1], 'args': ['-m', 'pa', 'mcp'], 'env': {'PYTHONPATH': sys.argv[2]}}
p.write_text(json.dumps(data, indent=2), encoding='utf-8')
print('Added pa-notes to', p)
"@
& $py -c $code $py $root
Write-Host "Restart Claude Code. Then try: /pa-today"
