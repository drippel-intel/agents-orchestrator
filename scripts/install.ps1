# One-shot installer for bi-orchestrator on a fresh Windows machine.
#
# Idempotent -- safe to re-run. Default behaviour:
#   1. Verify Python 3.10+ is on PATH.
#   2. Create / reuse a venv (default: ~/.bi-orchestrator-venv).
#   3. Install the project from the surrounding source folder in editable mode.
#   4. Register the MCP server in ~/.cursor/mcp.json.
#   5. Install the chat skill in ~/.cursor/skills/bi-orchestrator/.
#   6. Check CURSOR_API_KEY -- if absent, prompt and persist it via `setx`.
#
# Opt out of any of those defaults with the corresponding -Skip... switch.
#
# Usage:
#   .\scripts\install.ps1                                   # full install
#   .\scripts\install.ps1 -SkipMcp -SkipSkill               # venv + pip only
#   .\scripts\install.ps1 -VenvPath D:\envs\bi-orch-venv    # custom venv path
#   .\scripts\install.ps1 -SkipApiKeyPrompt                 # don't ask for key
#   .\scripts\install.ps1 -ApiKey 'crsr_...'                # non-interactive key
#
# Notes:
#   - Pick a venv path *outside* any OneDrive-synced folder; sync locks collide
#     with venv writes.
#   - This script does NOT install Python. If `python --version` fails, install
#     Python 3.10+ first (`winget install Python.Python.3.12` or
#     https://www.python.org/downloads/).
#   - `setx` persists the API key for future shells only; the current process
#     is also updated so the rest of this script sees it.

[CmdletBinding()]
param(
    [string] $VenvPath = "$env:USERPROFILE\.bi-orchestrator-venv",
    [switch] $SkipMcp,
    [switch] $SkipSkill,
    [switch] $SkipApiKeyPrompt,
    [string] $ApiKey,
    [switch] $SkipPipUpgrade
)

$ErrorActionPreference = "Stop"

function Write-Header($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

function Show-SecretInputDialog {
    # Pops a native WinForms password dialog with a masked textbox so paste
    # (Ctrl+V / right-click) works reliably regardless of which terminal host
    # is running this script. Returns the entered string, or empty on cancel.
    # Throws on systems where Windows.Forms isn't usable -- caller falls back.
    param(
        [string] $Title  = "Enter value",
        [string] $Prompt = "Value:"
    )
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop

    $form               = New-Object System.Windows.Forms.Form
    $form.Text          = $Title
    $form.ClientSize    = New-Object System.Drawing.Size(520, 200)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox   = $false
    $form.MinimizeBox   = $false
    $form.Topmost       = $true
    $form.KeyPreview    = $true

    $label          = New-Object System.Windows.Forms.Label
    $label.Text     = $Prompt
    $label.Location = New-Object System.Drawing.Point(15, 15)
    $label.Size     = New-Object System.Drawing.Size(490, 60)
    $form.Controls.Add($label)

    $textBox                       = New-Object System.Windows.Forms.TextBox
    $textBox.UseSystemPasswordChar = $true
    $textBox.Location              = New-Object System.Drawing.Point(15, 85)
    $textBox.Size                  = New-Object System.Drawing.Size(490, 25)
    $form.Controls.Add($textBox)

    $showChk          = New-Object System.Windows.Forms.CheckBox
    $showChk.Text     = "Show"
    $showChk.Location = New-Object System.Drawing.Point(15, 120)
    $showChk.Size     = New-Object System.Drawing.Size(80, 25)
    $showChk.Add_CheckedChanged({ $textBox.UseSystemPasswordChar = -not $showChk.Checked })
    $form.Controls.Add($showChk)

    $okButton              = New-Object System.Windows.Forms.Button
    $okButton.Text         = "OK"
    $okButton.Location     = New-Object System.Drawing.Point(325, 155)
    $okButton.Size         = New-Object System.Drawing.Size(85, 30)
    $okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.AcceptButton     = $okButton
    $form.Controls.Add($okButton)

    $cancelButton              = New-Object System.Windows.Forms.Button
    $cancelButton.Text         = "Cancel"
    $cancelButton.Location     = New-Object System.Drawing.Point(420, 155)
    $cancelButton.Size         = New-Object System.Drawing.Size(85, 30)
    $cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.CancelButton         = $cancelButton
    $form.Controls.Add($cancelButton)

    $form.Add_Shown({ $textBox.Focus() })
    $result = $form.ShowDialog()
    $value  = $textBox.Text
    $form.Dispose()

    if ($result -ne [System.Windows.Forms.DialogResult]::OK) { return "" }
    return $value
}

function Read-SecretString($prompt) {
    # Prefer the WinForms dialog because paste into the Cursor integrated
    # terminal does not feed `[Console]::ReadKey` cleanly (bracketed paste
    # escape sequences get filtered as control chars). Fall back to a plain
    # visible `Read-Host` if the GUI surface isn't available.
    try {
        $msg = "Paste your Cursor API key below.`r`nMint one at: https://cursor.com/dashboard/cloud-agents`r`nThe value is masked; click 'Show' to reveal what you pasted."
        return (Show-SecretInputDialog -Title "bi-orchestrator: CURSOR_API_KEY" -Prompt $msg)
    } catch {
        Write-Warning "GUI prompt unavailable ($($_.Exception.Message)). Falling back to a visible text prompt -- the key will appear on screen."
        return (Read-Host -Prompt $prompt)
    }
}

function Set-CursorApiKey($key) {
    # Trim whitespace and strip accidental wrapping quotes from a paste.
    $key = $key.Trim().Trim('"').Trim("'")
    if (-not $key) { return $false }
    & setx CURSOR_API_KEY $key | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "setx failed (exit $LASTEXITCODE). Set it manually:  setx CURSOR_API_KEY '<key>'"
        return $false
    }
    # Also update the current process so the rest of this script sees it.
    $env:CURSOR_API_KEY = $key
    return $true
}

function Stop-LockingProcesses {
    # Stop any process whose image is one of $ExePaths. The typical hit is
    # `bi-orchestrator-mcp.exe` held by Cursor as a stdio child -- Cursor will
    # respawn it on the next tool call, so killing it is safe and avoids
    # `WinError 32` from pip when it tries to overwrite the script entry point.
    param([Parameter(Mandatory)] [string[]] $ExePaths)

    $targets = @()
    foreach ($exe in $ExePaths) {
        if (Test-Path $exe) {
            $targets += [System.IO.Path]::GetFullPath($exe).ToLowerInvariant()
        }
    }
    if (-not $targets) { return }

    $stopped = 0
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $img = $_.Path
            if (-not $img) { return }
            $imgNorm = [System.IO.Path]::GetFullPath($img).ToLowerInvariant()
            if ($targets -contains $imgNorm) {
                Write-Host "Stopping $($_.ProcessName) PID $($_.Id) -- it has $img locked." -ForegroundColor Yellow
                $_ | Stop-Process -Force -ErrorAction Stop
                $stopped++
            }
        } catch {
            # Path access denied for some system processes; ignore.
        }
    }
    if ($stopped -gt 0) {
        # Give Windows a beat to release the handles.
        Start-Sleep -Milliseconds 500
        Write-Host "Stopped $stopped locking process(es). Cursor will respawn the MCP on its next tool call." -ForegroundColor Yellow
    }
}

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Write-Header "bi-orchestrator install"
Write-Host "Project root : $ProjectRoot"
Write-Host "Venv path    : $VenvPath"
Write-Host "Install MCP  : $(-not $SkipMcp)"
Write-Host "Install skill: $(-not $SkipSkill)"

# --- 1. Python check ----------------------------------------------------------
Write-Header "Python"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python not found on PATH. Install Python 3.10+ and retry."
    exit 1
}
$pyVersion = & python --version 2>&1
Write-Host "Found: $pyVersion"

$ver = (& python -c "import sys; print('{}.{}'.format(sys.version_info[0], sys.version_info[1]))").Trim()
$parts = $ver.Split('.')
if (([int]$parts[0]) -lt 3 -or (([int]$parts[0]) -eq 3 -and ([int]$parts[1]) -lt 10)) {
    Write-Error "Python $ver is too old. Need 3.10 or newer."
    exit 1
}

# --- 2. Venv ------------------------------------------------------------------
Write-Header "Virtual environment"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
if (Test-Path $VenvPython) {
    Write-Host "Venv exists, reusing: $VenvPath"
} else {
    Write-Host "Creating venv at $VenvPath..."
    & python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed"; exit $LASTEXITCODE }
}

$VenvBiOrch    = Join-Path $VenvPath "Scripts\bi-orchestrator.exe"
$VenvBiOrchMcp = Join-Path $VenvPath "Scripts\bi-orchestrator-mcp.exe"

# Free up any locked entry-point exes before pip overwrites them. This is
# almost always the MCP server held by a running Cursor IDE session.
Stop-LockingProcesses -ExePaths @($VenvBiOrch, $VenvBiOrchMcp)

if (-not $SkipPipUpgrade) {
    Write-Host "Upgrading pip..."
    & $VenvPython -m pip install --upgrade pip --quiet
}

# --- 3. Install bi-orchestrator -----------------------------------------------
Write-Header "Install bi-orchestrator (editable mode)"
Push-Location $ProjectRoot
try {
    & $VenvPython -m pip install -e ".[dev]" --quiet
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit $LASTEXITCODE }
} finally {
    Pop-Location
}

Write-Host "Installed:"
& $VenvBiOrch --version

# --- 4. Register MCP + skill (default on) -------------------------------------
$wantMcp   = -not $SkipMcp
$wantSkill = -not $SkipSkill
if ($wantMcp -or $wantSkill) {
    Write-Header "Register with Cursor"
    $mcpArgs = @()
    if ($wantMcp)   { $mcpArgs += "install-mcp" }
    if ($wantSkill) { $mcpArgs += "--skill" }
    # If the user opted out of MCP but kept skill, fall through to the package
    # entry point with --skill only -- it still copies the skill.
    if (-not $wantMcp -and $wantSkill) {
        # The CLI requires the install-mcp subcommand even when only the skill
        # is wanted; we just pass --skill alongside.
        $mcpArgs = @("install-mcp", "--skill")
    }
    & $VenvBiOrch @mcpArgs
    Write-Host "Restart any open Cursor chats so the new MCP / skill is picked up."
} else {
    Write-Header "MCP / skill registration"
    Write-Host "Skipped (both -SkipMcp and -SkipSkill were set)."
}

$ApiKeyMinLength = 10

# --- 5. API key ---------------------------------------------------------------
Write-Header "CURSOR_API_KEY"
if ($ApiKey) {
    if (Set-CursorApiKey $ApiKey) {
        Write-Host "Stored via setx from -ApiKey parameter (length $($env:CURSOR_API_KEY.Length))." -ForegroundColor Green
    }
} elseif ($env:CURSOR_API_KEY -and $env:CURSOR_API_KEY.Length -ge $ApiKeyMinLength) {
    Write-Host "Already set in the current shell (length $($env:CURSOR_API_KEY.Length))." -ForegroundColor Green
} else {
    # Also check the persisted user-scope value -- `setx` from a previous run
    # would not be visible in this shell, only in newly-spawned ones.
    $persisted = [Environment]::GetEnvironmentVariable("CURSOR_API_KEY", "User")
    if ($env:CURSOR_API_KEY -and $env:CURSOR_API_KEY.Length -lt $ApiKeyMinLength) {
        Write-Warning ("Current shell CURSOR_API_KEY is only {0} char(s) -- treating it as junk and re-prompting." -f $env:CURSOR_API_KEY.Length)
        Remove-Item Env:\CURSOR_API_KEY -ErrorAction SilentlyContinue
    }
    if ($persisted -and $persisted.Length -lt $ApiKeyMinLength) {
        Write-Warning ("Persisted CURSOR_API_KEY is only {0} char(s) -- clearing it and re-prompting." -f $persisted.Length)
        [Environment]::SetEnvironmentVariable("CURSOR_API_KEY", $null, "User")
        $persisted = $null
    }
    if ($persisted) {
        Write-Host "Persisted via setx in a previous shell (length $($persisted.Length))." -ForegroundColor Green
        Write-Host "It will be visible after you open a fresh PowerShell."
        $env:CURSOR_API_KEY = $persisted
    } elseif ($SkipApiKeyPrompt) {
        Write-Warning "CURSOR_API_KEY is not set and -SkipApiKeyPrompt was passed."
        Write-Host "Set it later with:  setx CURSOR_API_KEY '<key>'"
    } else {
        Write-Host "CURSOR_API_KEY is not set on this machine." -ForegroundColor Yellow
        Write-Host "Mint one at:  https://cursor.com/dashboard/cloud-agents  (User API Keys > New API Key)"
        Write-Host "Opening a dialog -- paste the key (Ctrl+V) and click OK." -ForegroundColor Cyan
        Write-Host "(If no dialog appears, the script will fall back to a visible terminal prompt.)"
        $entered = Read-SecretString "CURSOR_API_KEY"
        $entered = $entered.Trim().Trim('"').Trim("'")
        if (-not $entered) {
            Write-Warning "No key entered. Set it later with:  setx CURSOR_API_KEY '<key>'"
        } elseif ($entered.Length -lt 10) {
            Write-Warning "Only $($entered.Length) character(s) captured -- that doesn't look like a real key."
            Write-Host  "If your terminal mangled the input, retry from a stock PowerShell window, or"
            Write-Host  "pass the key non-interactively:  .\scripts\install.ps1 -ApiKey 'crsr_...'"
        } else {
            Write-Host ("Captured {0} characters (starts with '{1}')." -f $entered.Length, $entered.Substring(0, [Math]::Min(5, $entered.Length))) -ForegroundColor Cyan
            $confirm = Read-Host "Persist via setx? [Y/n]"
            if (-not $confirm) { $confirm = "Y" }
            if ($confirm -match '^[Yy]') {
                if (Set-CursorApiKey $entered) {
                    Write-Host "Stored via setx (length $($env:CURSOR_API_KEY.Length))." -ForegroundColor Green
                    Write-Host "Open a fresh PowerShell to verify:  echo `$env:CURSOR_API_KEY"
                }
            } else {
                Write-Warning "Cancelled. Set it later with:  setx CURSOR_API_KEY '<key>'"
            }
        }
    }
}

# --- 6. Final summary ---------------------------------------------------------
Write-Header "Next steps"
Write-Host "Try the smoke flow:" -ForegroundColor Cyan
Write-Host "  $VenvBiOrch smoke --target-repo <absolute-path-to-bi-repo>"
Write-Host ""
Write-Host "Daily entry points in this venv:"
Write-Host "  $VenvBiOrch       # CLI (smoke, status, install-mcp, daemon)"
Write-Host "  $VenvBiOrchMcp    # MCP server (Cursor spawns this; do not run directly)"
