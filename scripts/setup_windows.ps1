param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot
& $Python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 x64 required"; assert sys.maxsize > 2**32, "64-bit required"'
if ($LASTEXITCODE -ne 0) { throw 'Select a Python 3.12 x64 executable with -Python.' }
$runtimeDirs = @('tools/BallonsTranslator/.venv', 'tools/magi-pilot/.venv')
foreach ($relative in $runtimeDirs) {
    if (Test-Path -LiteralPath (Join-Path $repoRoot $relative)) {
        throw "Existing environment preserved: $relative. Use a fresh clone or the manual commands in docs/SETUP.zh-TW.md."
    }
}
foreach ($relative in $runtimeDirs) {
    & $Python -m venv $relative
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed: $relative" }
    $runtimePython = Join-Path $repoRoot "$relative/Scripts/python.exe"
    & $runtimePython -m pip install torch==2.14.0+cpu torchvision==0.29.0+cpu --index-url https://download.pytorch.org/whl/cpu
    if ($LASTEXITCODE -ne 0) { throw "CPU PyTorch installation failed: $relative" }
    $requirements = if ($relative -eq 'tools/BallonsTranslator/.venv') { 'requirements-local.txt' } else { 'tools/magi-pilot/requirements-resolved.txt' }
    & $runtimePython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "Requirements installation failed: $relative" }
}
Write-Host 'Python environments installed. Next: read model terms, prepare_models.py --download, import Sakura, then preflight.py.'
