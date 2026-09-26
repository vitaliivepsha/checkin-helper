# watchdog.ps1 - keeps bot.py alive across crashes and tells the owner on
# Telegram whenever it had to restart it.
#
# Run THIS instead of `python bot.py` directly. Why a separate script and
# not just code inside bot.py: the whole point is surviving the bot's own
# process dying, so the thing doing the restarting/notifying can never be
# part of that same process - if it were, it would die right along with it
# and never get a chance to notice or tell anyone.
#
# The Telegram notification works even though the bot's own polling loop
# is down: sendMessage is a plain HTTPS call to Telegram's API using the
# same bot token from .env - it doesn't need the bot's own process to be
# running or polling, only the token and the owner's chat id (same
# AUTO_TOAST_OWNER_ID bot.py itself defaults to).
#
# A manual /restart from Telegram (bot.py's own owner-only command) does
# NOT trigger a notification here - it calls os.execv, which replaces the
# process image IN PLACE (same PID) rather than exiting, so this script's
# `Wait` never sees it as an exit. Only a genuine crash (or an actual
# `sys.exit`) does.
#
# Usage: right-click -> "Run with PowerShell", or from a terminal:
#   powershell -ExecutionPolicy Bypass -File watchdog.ps1
# Leave the window open (or run it as a scheduled task / minimized) -
# closing it kills bot.py too, same as closing a terminal running it
# directly would.

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Get-EnvValue {
    param([string]$Name, [string]$Default = $null)
    if (-not (Test-Path ".env")) { return $Default }
    $line = Get-Content ".env" | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if (-not $line) { return $Default }
    return ($line -replace "^$Name=", "").Trim()
}

$botToken = Get-EnvValue -Name "TELEGRAM_BOT_TOKEN"
# Mirrors bot.py's own AUTO_TOAST_OWNER_ID default - keep these two in sync
# if that default ever changes.
$ownerId = Get-EnvValue -Name "AUTO_TOAST_OWNER_ID" -Default "402733193"

function Send-OwnerNotification {
    param([string]$Text)
    if (-not $botToken -or -not $ownerId) {
        Write-Host "No TELEGRAM_BOT_TOKEN/owner id available - skipping notification."
        return
    }
    try {
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendMessage" -Method Post -Body @{
            chat_id = $ownerId
            text    = $Text
        } -TimeoutSec 10 | Out-Null
    } catch {
        Write-Host "Failed to send Telegram notification: $_"
    }
}

Write-Host "watchdog.ps1 started - watching bot.py in $scriptDir"
$isFirstStart = $true

while ($true) {
    $startedAt = Get-Date
    Write-Host "[$startedAt] Starting bot.py..."

    if (-not $isFirstStart) {
        Send-OwnerNotification "Перезапускаю бота (попередній процес завершився)..."
    }
    $isFirstStart = $false

    $proc = Start-Process -FilePath "python" -ArgumentList "bot.py" -NoNewWindow -PassThru -Wait
    $exitCode = $proc.ExitCode
    $ranFor = (Get-Date) - $startedAt

    $msg = "Bot exited with code $exitCode after $([int]$ranFor.TotalMinutes) min."
    Write-Host "[$(Get-Date)] $msg"
    Send-OwnerNotification "Бот несподівано зупинився (код виходу: $exitCode, працював $([int]$ranFor.TotalMinutes) хв). Перезапускаю за 5с..."

    Start-Sleep -Seconds 5
}
