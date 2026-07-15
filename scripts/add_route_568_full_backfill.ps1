$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$OutputFile = Join-Path $env:TEMP "ADD_ROUTE_568_FULL_BACKFILL.txt"
$PatchFile = Join-Path $env:TEMP "add_route_568.py"

"==================== ADD ROUTE 568 + FULL BACKFILL ====================" | Set-Content $OutputFile -Encoding UTF8
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
    cmd.exe /d /s /c $CommandLine 2>&1 | Tee-Object -FilePath $OutputFile -Append
    if ($LASTEXITCODE -ne 0) {
        Fail "$Name failed with exit code $LASTEXITCODE"
    }
}

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + "$env:APPDATA\npm"

Run-Step "CHECK GIT" "where.exe git && git --version"
Run-Step "CHECK PYTHON" "where.exe python && python --version"
Run-Step "CHECK RAILWAY" "where.exe railway.cmd && railway.cmd --version"

Log ""
Log "==================== SYNC CODE ===================="
git fetch origin 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "git fetch failed" }
git reset --hard origin/main 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "git reset failed" }
git pull origin main 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "git pull failed" }

@'
from pathlib import Path
import importlib.util
import pprint

SOURCE_CHAT = -1003894275482
DEST_CHAT = -1003918958200
DEST_TOPIC = 568
ROUTE_NAME = "Mirror Route568 3894275482"

routes_path = Path("telegram_worker/routes.py")
spec = importlib.util.spec_from_file_location("routes_mod", routes_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
routes = list(mod.ROUTES)

exists = any(
    int(r.get("source_chat")) == SOURCE_CHAT
    and r.get("source_topic") is None
    and int(r.get("dest_chat")) == DEST_CHAT
    and int(r.get("dest_topic")) == DEST_TOPIC
    for r in routes
)

if not exists:
    routes.append({
        "name": ROUTE_NAME,
        "source_chat": SOURCE_CHAT,
        "source_topic": None,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "verify_title": False,
    })

text = (
    "import os\n\n"
    "# telegram_worker/routes.py\n"
    "# source_topic=None means all messages from that source.\n\n"
    "ROUTES = "
    + pprint.pformat(routes, width=120, sort_dicts=False)
    + "\n\n"
    + 'if os.environ.get("DISABLE_PROVIDER_ROUTES", "0").strip() == "1":\n'
    + "    ROUTES = []\n"
)
routes_path.write_text(text, encoding="utf-8")

hub = Path("telegram_worker/worker_signal_hub.py").read_text(encoding="utf-8-sig")
if "568" not in hub:
    raise SystemExit("AI formatter does not appear to allow topic 568")

print(f"ROUTE_EXISTS_BEFORE={exists}")
print(f"ROUTE_READY source={SOURCE_CHAT} dest={DEST_CHAT}_{DEST_TOPIC}")
print("AI_TOPIC_568_PRESENT=True")
'@ | Set-Content $PatchFile -Encoding UTF8

Run-Step "PATCH ROUTE" "python -X utf8 `"$PatchFile`""

Log ""
Log "==================== VERIFY ROUTE ===================="
Select-String -Path "telegram_worker\routes.py" -Pattern "Mirror Route568 3894275482|-1003894275482|dest_topic': 568" | Tee-Object -FilePath $OutputFile -Append
Select-String -Path "telegram_worker\worker_signal_hub.py" -Pattern "DEFAULT_ALLOWED_TOPICS.*568" | Tee-Object -FilePath $OutputFile -Append

Run-Step "PYTHON COMPILE" "python -m py_compile telegram_worker\routes.py telegram_worker\worker_fixed.py telegram_worker\worker_signal_hub.py"

Log ""
Log "==================== COMMIT + PUSH ===================="
$Changes = git status --porcelain
if ($Changes) {
    git add telegram_worker\routes.py 2>&1 | Tee-Object -FilePath $OutputFile -Append
    git commit -m "Add mirror route 568 from 3894275482" 2>&1 | Tee-Object -FilePath $OutputFile -Append
    if ($LASTEXITCODE -ne 0) { Fail "git commit failed" }
    git pull --rebase origin main 2>&1 | Tee-Object -FilePath $OutputFile -Append
    if ($LASTEXITCODE -ne 0) { Fail "git pull --rebase failed" }
    git push origin main 2>&1 | Tee-Object -FilePath $OutputFile -Append
    if ($LASTEXITCODE -ne 0) { Fail "git push failed" }
} else {
    Log "No code changes required; route already existed."
}

Log ""
Log "==================== ENABLE ONE-TIME FULL BACKFILL ===================="
$DebugChats = "-1003812195730,-1003371106919,-1003651353503,-1003087047858,-1002817163788,-1003770573057,-1002186832814,-1003894275482"

railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_DEBUG_CHATS=$DebugChats" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_DEBUG_CHATS failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_POLLING_ENABLED=1" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_POLLING_ENABLED failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ON_START=1" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_BACKFILL_ON_START failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_LIMIT=50000" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_BACKFILL_LIMIT failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ONLY_CHATS=-1003894275482" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_BACKFILL_ONLY_CHATS failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS=568" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "BLOCKED_SENDER_IDS=7556281143" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "setting BLOCKED_SENDER_IDS failed" }

Run-Step "DEPLOY RAW FORWARDER" "railway.cmd up --service imperium-telegram-worker --detach"

Log ""
Log "==================== WAIT FOR FULL BACKFILL ===================="
Log "The script will monitor Railway for up to 60 minutes. Keep this window open."

$Completed = $false
for ($i = 1; $i -le 120; $i++) {
    Start-Sleep -Seconds 30
    $Logs = railway.cmd logs --service "imperium-telegram-worker" 2>&1
    $Relevant = $Logs | Select-String -Pattern "Mirror Route568 3894275482|-1003894275482|startup_backfill|new mirror poll init|Traceback|SyntaxError|fatal crash|AuthKeyDuplicatedError|ApiIdInvalidError" | Select-Object -Last 80

    Log ""
    Log "--- BACKFILL CHECK $i/120 ---"
    $Relevant | Tee-Object -FilePath $OutputFile -Append

    if (($Logs -join "`n") -match "\[new mirror poll init\].*Mirror Route568 3894275482.*backfill=True") {
        $Completed = $true
        Log "FULL_BACKFILL_COMPLETED=True"
        break
    }

    if (($Logs -join "`n") -match "AuthKeyDuplicatedError|ApiIdInvalidError|SyntaxError|fatal crash") {
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
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_ON_START=0" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "disabling startup backfill failed" }
railway.cmd variables --service "imperium-telegram-worker" --set "NEW_MIRROR_BACKFILL_LIMIT=3" 2>&1 | Tee-Object -FilePath $OutputFile -Append
if ($LASTEXITCODE -ne 0) { Fail "resetting backfill limit failed" }

Run-Step "REDEPLOY RAW WITH BACKFILL OFF" "railway.cmd up --service imperium-telegram-worker --detach"
Start-Sleep -Seconds 60

Log ""
Log "==================== FINAL STATUS ===================="
railway.cmd status 2>&1 | Tee-Object -FilePath $OutputFile -Append
$FinalLogs = railway.cmd logs --service "imperium-telegram-worker" 2>&1
$FinalLogs | Select-String -Pattern "Mirror Route568 3894275482|-1003894275482|dest=-1003918958200_568|new mirror poll init|new mirror poll copied|Watching|Loaded|Traceback|SyntaxError|fatal crash|error|failed" | Select-Object -Last 180 | Tee-Object -FilePath $OutputFile -Append

Log ""
Log "RESULT: ROUTE_568_ADDED_AND_FULL_BACKFILL_COMPLETED"
Log "Source: -1003894275482"
Log "Destination: -1003918958200_568"
Log "DONE. Report copied and opened. Paste it here."

Get-Content $OutputFile -Raw | Set-Clipboard
notepad $OutputFile
