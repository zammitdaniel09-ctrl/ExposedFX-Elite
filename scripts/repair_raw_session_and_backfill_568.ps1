$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$SessionDir = Join-Path $env:USERPROFILE "ExposedFX_Telegram_Sessions"
$B64File = Join-Path $SessionDir "raw_forwarder.b64"

Write-Host ""
Write-Host "==================== RAW SESSION REPAIR + ROUTE 568 BACKFILL ===================="
Write-Host "You will only be asked for your Telegram phone number and login code."
Write-Host "Do not close this window."
Write-Host ""

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + "$env:APPDATA\npm"

Write-Host "[1/7] Pulling latest code..."
git fetch origin
git reset --hard origin/main

Write-Host "[2/7] Creating a fresh RAW Telegram session..."
New-Item -ItemType Directory -Force -Path $SessionDir | Out-Null
Remove-Item $B64File -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $SessionDir "fresh_raw_forwarder.session") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $SessionDir "fresh_raw_forwarder.session-journal") -Force -ErrorAction SilentlyContinue

& railway.cmd run --service "imperium-telegram-worker" -- python "scripts\make_telegram_session.py" "raw_forwarder" "$SessionDir"
if ($LASTEXITCODE -ne 0) { throw "Telegram session creation failed." }
if (!(Test-Path $B64File)) { throw "The new raw_forwarder.b64 file was not created." }

Write-Host "[3/7] Uploading the fresh session to Railway..."
$B64 = (Get-Content $B64File -Raw).Trim()
$ChunkSize = 3500
$Chunks = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $B64.Length; $i += $ChunkSize) {
    $Length = [Math]::Min($ChunkSize, $B64.Length - $i)
    $Chunks.Add($B64.Substring($i, $Length))
}

& railway.cmd variables --service "imperium-telegram-worker" --set "SESSION_B64_CHUNKS=$($Chunks.Count)"
if ($LASTEXITCODE -ne 0) { throw "Failed to set SESSION_B64_CHUNKS." }

for ($i = 0; $i -lt $Chunks.Count; $i++) {
    $Number = $i + 1
    Write-Host "Uploading session chunk $Number/$($Chunks.Count)..."
    & railway.cmd variables --service "imperium-telegram-worker" --set "SESSION_B64_$Number=$($Chunks[$i])"
    if ($LASTEXITCODE -ne 0) { throw "Failed to upload SESSION_B64_$Number." }
}

Write-Host "[4/7] Setting overlap protection and full backfill..."
$DebugChats = "-1003812195730,-1003371106919,-1003651353503,-1003087047858,-1002817163788,-1003770573057,-1002186832814,-1003894275482"
& railway.cmd variables --service "imperium-telegram-worker" --set "TELEGRAM_CONNECT_DELAY_SECONDS=90"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_DEBUG_CHATS=$DebugChats"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_POLLING_ENABLED=1"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ON_START=1"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_LIMIT=50000"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ONLY_CHATS=-1003894275482"
& railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS=568"
& railway.cmd variables --service "imperium-telegram-worker" --set "BLOCKED_SENDER_IDS=7556281143"

Write-Host "[5/7] Deploying the raw forwarder..."
& railway.cmd up --service "imperium-telegram-worker" --detach
if ($LASTEXITCODE -ne 0) { throw "Railway deployment failed." }

Write-Host "[6/7] Waiting 210 seconds for the protected startup and backfill..."
Start-Sleep -Seconds 210

Write-Host "[7/7] Latest route/session logs:"
& railway.cmd logs --service "imperium-telegram-worker" |
    Select-String -Pattern "Telegram deployment overlap protection|Logged in as|Mirror Route568 3894275482|startup_backfill|new mirror poll copied|new mirror poll init|AuthKeyDuplicatedError|Traceback|fatal crash|error|failed" |
    Select-Object -Last 180

Write-Host ""
Write-Host "DONE. Paste the displayed logs into ChatGPT."
