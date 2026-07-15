$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$OutputFile = Join-Path $env:TEMP "ADD_ROUTE_568_FULL_BACKFILL_V2.txt"
$PatchFile = Join-Path $env:TEMP "add_route_568_v2.py"

"==================== ADD ROUTE 568 + FULL BACKFILL V2 ====================" | Set-Content $OutputFile -Encoding UTF8
"Time: $(Get-Date)" | Add-Content $OutputFile -Encoding UTF8
"Folder: $(Get-Location)" | Add-Content $OutputFile -Encoding UTF8

function Log([string]$Text) {
    $Text | Tee-Object -FilePath $OutputFile -Append
}

function Fail([string]$Text) {
    Log ""
    Log "FAILED: $Text"
    Get-Content $OutputFile -Raw | Set-Clipboard
    notepad $OutputFile
    exit 1
}

function Run-Step([string]$Name, [string]$CommandLine) {
    Log ""
    Log "==================== $Name ===================="
    Log "COMMAND: $CommandLine"
    $TempOutput = Join-Path $env:TEMP "exposedfx_route568_step.txt"
    cmd.exe /d /s /c "$CommandLine > `"$TempOutput`" 2>&1"
    $ExitCode = $LASTEXITCODE
    if (Test-Path $TempOutput) {
        Get-Content $TempOutput -Raw | Tee-Object -FilePath $OutputFile -Append
    }
    if ($ExitCode -ne 0) {
        Fail "$Name failed with exit code $ExitCode"
    }
}

function Set-RailwayVar([string]$Key, [string]$Value) {
    Log "Setting $Key"
    $Result = & railway.cmd variables --service "imperium-telegram-worker" --set "${Key}=${Value}" 2>&1
    $ExitCode = $LASTEXITCODE
    if ($Result) { ($Result -join "`n") | Add-Content $OutputFile -Encoding UTF8 }
    if ($ExitCode -ne 0) { Fail "Setting $Key failed with exit code $ExitCode" }
}

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + "$env:APPDATA\npm"

Run-Step "CHECK GIT" "where.exe git && git --version"
Run-Step "CHECK PYTHON" "where.exe python && python --version"
Run-Step "CHECK RAILWAY" "where.exe railway.cmd && railway.cmd --version"
Run-Step "GIT FETCH" "git fetch origin"
Run-Step "GIT RESET" "git reset --hard origin/main"
Run-Step "GIT PULL" "git pull origin main"

@'
from pathlib import Path

SOURCE_CHAT = -1003894275482
DEST_CHAT = -1003918958200
DEST_TOPIC = 568
ROUTE_NAME = "Mirror Route568 3894275482"

path = Path("telegram_worker/routes.py")
text = path.read_text(encoding="utf-8-sig")

if str(SOURCE_CHAT) in text and ROUTE_NAME in text:
    print("ROUTE_EXISTS_BEFORE=True")
else:
    marker = "\n]\n\nif os.environ.get(\"DISABLE_PROVIDER_ROUTES\""
    pos = text.rfind(marker)
    if pos < 0:
        raise SystemExit("Could not find the end of ROUTES in routes.py")

    route = (
        ",\n {'name': 'Mirror Route568 3894275482',\n"
        "  'source_chat': -1003894275482,\n"
        "  'source_topic': None,\n"
        "  'dest_chat': -1003918958200,\n"
        "  'dest_topic': 568,\n"
        "  'verify_title': False}"
    )
    text = text[:pos] + route + text[pos:]
    path.write_text(text, encoding="utf-8")
    print("ROUTE_EXISTS_BEFORE=False")

hub = Path("telegram_worker/worker_signal_hub.py").read_text(encoding="utf-8-sig")
if "DEFAULT_ALLOWED_TOPICS" not in hub or "568" not in hub:
    raise SystemExit("AI formatter does not appear to allow destination topic 568")

print(f"ROUTE_READY source={SOURCE_CHAT} dest={DEST_CHAT}_{DEST_TOPIC}")
print("AI_TOPIC_568_PRESENT=True")
'@ | Set-Content $PatchFile -Encoding UTF8

Run-Step "PATCH ROUTE" "python -X utf8 `"$PatchFile`""

Log ""
Log "==================== VERIFY ROUTE ===================="
$RouteMatches = Select-String -Path "telegram_worker\routes.py" -Pattern "Mirror Route568 3894275482|-1003894275482|dest_topic': 568"
$RouteMatches | Tee-Object -FilePath $OutputFile -Append
if (-not ($RouteMatches | Select-String -Pattern "Mirror Route568 3894275482")) { Fail "Route 568 verification failed" }

Run-Step "PYTHON COMPILE" "python -m py_compile telegram_worker\routes.py telegram_worker\worker_fixed.py telegram_worker\worker_signal_hub.py"

$Changes = git status --porcelain
if ($Changes) {
    Run-Step "GIT ADD" "git add telegram_worker/routes.py"
    Run-Step "GIT COMMIT" "git commit -m `"Add mirror route 568 from 3894275482`""
    Run-Step "GIT REBASE" "git pull --rebase origin main"
    Run-Step "GIT PUSH" "git push origin main"
} else {
    Log "No code changes required; route already existed."
}

Log ""
Log "==================== ENABLE ONE-TIME FULL BACKFILL ===================="
$DebugChats = "-1003812195730,-1003371106919,-1003651353503,-1003087047858,-1002817163788,-1003770573057,-1002186832814,-1003894275482"
Set-RailwayVar "NEW_MIRROR_DEBUG_CHATS" $DebugChats
Set-RailwayVar "NEW_MIRROR_POLLING_ENABLED" "1"
Set-RailwayVar "NEW_MIRROR_BACKFILL_ON_START" "1"
Set-RailwayVar "NEW_MIRROR_BACKFILL_LIMIT" "50000"
Set-RailwayVar "NEW_MIRROR_BACKFILL_ONLY_CHATS" "-1003894275482"
Set-RailwayVar "NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS" "568"
Set-RailwayVar "BLOCKED_SENDER_IDS" "7556281143"

Run-Step "DEPLOY RAW FORWARDER" "railway.cmd up --service imperium-telegram-worker --detach"

Log ""
Log "==================== WAIT FOR FULL BACKFILL ===================="
Log "Monitoring Railway for up to 60 minutes. Keep this PowerShell window open."

$Completed = $false
for ($i = 1; $i -le 120; $i++) {
    Start-Sleep -Seconds 30
    $Logs = & railway.cmd logs --service "imperium-telegram-worker" 2>&1
    $LogText = $Logs -join "`n"
    $Relevant = $Logs | Select-String -Pattern "Mirror Route568 3894275482|-1003894275482|startup_backfill|new mirror poll init|new mirror poll copied|Traceback|SyntaxError|fatal crash|AuthKeyDuplicatedError|ApiIdInvalidError" | Select-Object -Last 120

    Log ""
    Log "--- BACKFILL CHECK $i/120 ---"
    $Relevant | Tee-Object -FilePath $OutputFile -Append

    if ($LogText -match "\[new mirror poll init\].*Mirror Route568 3894275482.*backfill=True") {
        $Completed = $true
        Log "FULL_BACKFILL_COMPLETED=True"
        break
    }

    if ($LogText -match "AuthKeyDuplicatedError|ApiIdInvalidError|SyntaxError|fatal crash") {
        Fail "Railway worker crashed during backfill"
    }
}

if (-not $Completed) {
    Log "FULL_BACKFILL_COMPLETED=NOT_CONFIRMED_WITHIN_60_MINUTES"
    Log "Backfill remains enabled so it can continue. Paste this report before restarting anything."
    Get-Content $OutputFile -Raw | Set-Clipboard
    notepad $OutputFile
    exit 2
}

Log ""
Log "==================== DISABLE STARTUP BACKFILL ===================="
Set-RailwayVar "NEW_MIRROR_BACKFILL_ON_START" "0"
Set-RailwayVar "NEW_MIRROR_BACKFILL_LIMIT" "3"
Run-Step "REDEPLOY RAW WITH BACKFILL OFF" "railway.cmd up --service imperium-telegram-worker --detach"
Start-Sleep -Seconds 60

Log ""
Log "==================== FINAL STATUS ===================="
$Status = & railway.cmd status 2>&1
if ($Status) { ($Status -join "`n") | Tee-Object -FilePath $OutputFile -Append }
$FinalLogs = & railway.cmd logs --service "imperium-telegram-worker" 2>&1
$FinalLogs | Select-String -Pattern "Mirror Route568 3894275482|-1003894275482|dest=-1003918958200_568|new mirror poll init|new mirror poll copied|Watching|Loaded|Traceback|SyntaxError|fatal crash|error|failed" | Select-Object -Last 200 | Tee-Object -FilePath $OutputFile -Append

Log ""
Log "RESULT: ROUTE_568_ADDED_AND_FULL_BACKFILL_COMPLETED"
Log "Source: -1003894275482"
Log "Destination: -1003918958200_568"
Log "DONE. Report copied and opened. Paste it here."

Get-Content $OutputFile -Raw | Set-Clipboard
notepad $OutputFile
