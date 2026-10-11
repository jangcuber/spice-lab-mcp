# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
# spice-lab-mcp installer (Windows PowerShell 5.1+).
# Creates .venv, installs pinned dependencies, checks the Cadence installation,
# and prints (or with -Register, runs) the MCP client registration commands.
param(
    [switch]$Register,
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    # Returns @(executable, extra-args...) for a Python >= 3.10.
    $candidates = @()
    if ($Python) { $candidates += ,@($Python) }
    $candidates += ,@("py", "-3.13")
    $candidates += ,@("py", "-3.12")
    $candidates += ,@("python")
    foreach ($c in $candidates) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        try {
            $version = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version -and [version]$version -ge [version]"3.10") { return ,$c }
        } catch { }
    }
    throw "Python 3.10+ not found. Install it from python.org, or pass -Python C:\path\to\python.exe"
}

$py = Find-Python
$pyExe = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)
Write-Host "Using Python: $($py -join ' ')"
if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) {
    & $pyExe @pyArgs -m venv "$Root\.venv"
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$venvPy = "$Root\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r "$Root\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "`nChecking Cadence installation..."
& $venvPy -c "from core import Settings; s=Settings(); print('cadence_bin :', s.bin); print('capture.exe :', s.apps['capture'].is_file()); print('pspice.exe  :', s.apps['probe'].is_file()); print('eval.lib    :', s.library.is_file())"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Cadence not detected. Copy config.example.json to config.json and set cadence_bin / model_library."
}

$server = "$Root\server.py"
$claudeCmd = "claude mcp add --scope user spice-lab -- `"$venvPy`" `"$server`""
$codexCmd  = "codex mcp add spice-lab -- `"$venvPy`" `"$server`""
Write-Host "`nRegister with your MCP client:"
Write-Host "  Claude Code : $claudeCmd"
Write-Host "  Codex CLI   : $codexCmd"
Write-Host "  Other (JSON): { `"command`": `"$($venvPy -replace '\\','\\')`", `"args`": [`"$($server -replace '\\','\\')`"] }"

if ($Register) {
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Invoke-Expression $claudeCmd
    } else {
        Write-Warning "claude CLI not on PATH; add the JSON entry above to ~/.claude.json under mcpServers."
    }
}
Write-Host "`nDone. Restart your MCP client / start a new session to load the tools."
