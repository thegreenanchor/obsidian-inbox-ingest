# run_inbox_ingest.ps1 — Platform-independent execution wrapper
# Runs the standalone Obsidian inbox ingestion script using relative paths

# Get script directory using PowerShell special variable
$ScriptDir = $PSScriptRoot
$LogFile   = "$ScriptDir\inbox_ingest.log"

# Search paths for Python executable (looks for local virtual environment first, falls back to system python)
$VenvPython = "$ScriptDir\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "$ScriptDir\venv\Scripts\python.exe"
}
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "$ScriptDir\.venv\bin\python"
}

if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    # Fallback to system python path
    $Python = "python"
}

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Starting inbox compile execution" | Tee-Object -FilePath $LogFile -Append

Set-Location $ScriptDir

# Enforce UTF-8 coding environment
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Execute the ingest script
& $Python "$ScriptDir\ingest_standalone.py" 2>&1 | Tee-Object -FilePath $LogFile -Append
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Compile complete`n" | Tee-Object -FilePath $LogFile -Append
} else {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Compile failed with exit code $ExitCode`n" | Tee-Object -FilePath $LogFile -Append
}

exit $ExitCode
