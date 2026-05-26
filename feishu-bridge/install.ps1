# Feishu-Claude Bridge — Setup Script
# Run this in PowerShell to set up the bridge from scratch.

param(
    [switch]$Help
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host ">>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg){ Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  [X] $msg" -ForegroundColor Red; exit 1 }

if ($Help) {
    Write-Host @"
Feishu-Claude Bridge — Setup

Sets up the bridge so you can chat with Claude via Feishu group messages.

Prerequisites:
  1. Python 3.8+ installed and on PATH
  2. Claude Code CLI installed (npm install -g @anthropic-ai/claude-code)
  3. A Feishu bot app with:
     - App ID & App Secret
     - im:message scope enabled
     - Added to a group as a bot member

Usage:
  .\install.ps1              # Interactive setup
  .\install.ps1 -Help        # Show this help
"@
    exit 0
}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Feishu ↔ Claude Bridge — Setup"             -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check Python
Write-Step "Checking prerequisites..."
try {
    $pyVersion = python --version
    Write-OK $pyVersion
} catch {
    Write-Err "Python not found. Install Python 3.8+ and try again."
}

# 2. Install Python deps
Write-Step "Installing Python dependencies..."
try {
    python -m pip install requests -q
    Write-OK "requests installed"
} catch {
    Write-Err "Failed to install requests: $_"
}

# 3. Check Claude CLI
try {
    $claudeVer = claude --version 2>$null
    Write-OK "Claude CLI: $claudeVer"
} catch {
    Write-Warn "Claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
    $continue = Read-Host "Continue anyway? (y/N)"
    if ($continue -ne "y") { exit 1 }
}

# 4. Gather Feishu credentials
Write-Host ""
Write-Step "Feishu Bot Configuration"
Write-Host "Create a Feishu bot app at https://open.feishu.cn/app, then enter:"
Write-Host ""

$appId = Read-Host "  App ID"
$appSecret = Read-Host "  App Secret" -AsSecureString
$botId = Read-Host "  Bot App ID (from app detail page, e.g. cli_xxxx)"
$groupId = Read-Host "  Group Chat ID (oc_xxxx — the group the bot is added to)"

# Decrypt secure string
$BSTR = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($appSecret)
$plainSecret = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)

# 5. Write config.json
Write-Step "Writing config.json..."
$config = @{
    group_id       = $groupId
    bot_app_id     = $botId
    poll_interval  = 3
    state_file     = ".feishu_bridge_state.json"
    log_file       = "bridge_debug.log"
    credentials_file = ""
}

$configPath = Join-Path $PSScriptRoot "config.json"
$config | ConvertTo-Json | Set-Content $configPath -Encoding UTF8
Write-OK "config.json created"

# 6. Write credentials.json
$credsDir = Join-Path $env:USERPROFILE ".feishu-user-plugin"
$credsPath = Join-Path $credsDir "credentials.json"

if (-not (Test-Path $credsDir)) {
    New-Item -ItemType Directory -Path $credsDir -Force | Out-Null
}

$creds = @{
    profiles = @{
        default = @{
            LARK_APP_ID     = $appId
            LARK_APP_SECRET = $plainSecret
        }
    }
}
$creds | ConvertTo-Json -Depth 3 | Set-Content $credsPath -Encoding UTF8
Write-OK "Credentials saved to $credsPath"

# 7. Optional: scheduled task
Write-Host ""
$autoStart = Read-Host "Set up auto-start on login? (Y/n)"
if ($autoStart -ne "n") {
    $vbsPath = Join-Path $PSScriptRoot "start.vbs"
    $taskName = "FeishuClaudeBridge"

    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force
    Write-OK "Scheduled task '$taskName' created — bridge will start automatically on login."
}

# 8. Done
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Setup complete!"                              -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Start the bridge:"
Write-Host "  python bridge.py"
Write-Host "  (or double-click start.vbs to run silently)"
Write-Host ""
Write-Host "Test it: send a message in your Feishu group and wait for a reply."
Write-Host ""

$startNow = Read-Host "Start the bridge now? (Y/n)"
if ($startNow -ne "n") {
    $scriptDir = $PSScriptRoot
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python.exe"
    $psi.Arguments = "`"$scriptDir\bridge.py`""
    $psi.WorkingDirectory = $scriptDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    [System.Diagnostics.Process]::Start($psi) | Out-Null
    Write-OK "Bridge started in background (PID hidden)"
}
