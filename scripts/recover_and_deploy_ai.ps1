$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$OutputFile = Join-Path $env:TEMP "EXPOSEDFX_RECOVER_AND_DEPLOY_AI.txt"
"==================== EXPOSEDFX RECOVER AND DEPLOY AI ====================" | Set-Content $OutputFile -Encoding UTF8
"Time: $(Get-Date)" | Add-Content $OutputFile -Encoding UTF8
"Folder: $(Get-Location)" | Add-Content $OutputFile -Encoding UTF8

function Log($Text) {
    $Text | Tee-Object -FilePath $OutputFile -Append
}

function Run($Name, $Exe, $Args) {
    Log ""
    Log "==================== $Name ===================="
    Log "COMMAND: $Exe $Args"
    $tmp = Join-Path $env:TEMP "exposedfx_step_output.txt"
    cmd /c "$Exe $Args > `"$tmp`" 2>&1"
    if (Test-Path $tmp) { Get-Content $tmp -Raw | Add-Content $OutputFile -Encoding UTF8 }
    if ($LASTEXITCODE -ne 0) {
        Log "FAILED: $Name exit=$LASTEXITCODE"
        Get-Content $OutputFile -Raw | Set-Clipboard
        notepad $OutputFile
        exit $LASTEXITCODE
    }
}

$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + "$env:APPDATA\npm"

Log ""
Log "==================== POWERSHELL READY CHECK ===================="
Log "PowerShell is executing this script correctly."
Log "Prompt should NOT show >> while this is running."
Log "PSVersion: $($PSVersionTable.PSVersion)"
Log "User: $env:USERNAME"

Run "CHECK GIT" "where" "git"
Run "CHECK PYTHON" "where" "python"
Run "CHECK RAILWAY" "where" "railway.cmd"

Log ""
Log "==================== GIT SYNC ===================="
git status -sb 2>&1 | Add-Content $OutputFile -Encoding UTF8
git fetch origin 2>&1 | Add-Content $OutputFile -Encoding UTF8
git reset --hard origin/main 2>&1 | Add-Content $OutputFile -Encoding UTF8
git pull origin main 2>&1 | Add-Content $OutputFile -Encoding UTF8
git status -sb 2>&1 | Add-Content $OutputFile -Encoding UTF8
git log --oneline -10 2>&1 | Add-Content $OutputFile -Encoding UTF8

Log ""
Log "==================== VERIFY REQUIRED PATCHES ===================="
$checks = @(
    @{File="telegram_worker\universal_signal_ai.py"; Pattern="explicit_bracket_sl_price"},
    @{File="telegram_worker\universal_signal_ai.py"; Pattern="clean_tp_body_for_numbers"},
    @{File="telegram_worker\universal_signal_ai.py"; Pattern="Provider TPs first"},
    @{File="telegram_worker\universal_signal_ai.py"; Pattern="TP1 = 1:1"},
    @{File="telegram_worker\universal_signal_ai.py"; Pattern="sl_ref = validation_entry_ref"},
    @{File="telegram_worker\signal_refiner.py"; Pattern="open_idx = min"},
    @{File="telegram_worker\signal_refiner.py"; Pattern="4100-00.25"},
    @{File="telegram_worker\worker_signal_hub.py"; Pattern="do NOT include topic/provider key"},
    @{File="telegram_worker\worker_signal_hub.py"; Pattern="SEND_SOURCE_LINE"},
    @{File="telegram_worker\worker_signal_hub.py"; Pattern="FORWARD_SIGNAL_CANDIDATES"}
)
$missing = 0
foreach ($c in $checks) {
    $hit = Select-String -Path $c.File -Pattern $c.Pattern -SimpleMatch -ErrorAction SilentlyContinue
    if ($hit) { Log "OK: $($c.File) contains $($c.Pattern)" } else { Log "MISSING: $($c.File) lacks $($c.Pattern)"; $missing++ }
}
if ($missing -gt 0) {
    Log "FAILED: Required code patches are missing. Do not deploy."
    Get-Content $OutputFile -Raw | Set-Clipboard
    notepad $OutputFile
    exit 10
}

Run "PYTHON COMPILE" "python" "-m py_compile telegram_worker\routes.py telegram_worker\worker_fixed.py telegram_worker\worker_signal_hub.py telegram_worker\universal_signal_ai.py telegram_worker\signal_refiner.py telegram_worker\worker_clean_signal_forwarder.py"

Log ""
Log "==================== RAILWAY STATUS BEFORE ===================="
railway.cmd status 2>&1 | Add-Content $OutputFile -Encoding UTF8

Log ""
Log "==================== SET SAFE VARIABLES ===================="
$vars = @(
    @("exposedfx-ai-signal-formatter", "PURGE_DEST_ON_START=0"),
    @("exposedfx-ai-signal-formatter", "AUTO_TP_IF_MISSING=1"),
    @("exposedfx-ai-signal-formatter", "CONTENT_DEDUPE_ENABLED=1"),
    @("exposedfx-ai-signal-formatter", "SEND_SIGNAL_UPDATES=0"),
    @("exposedfx-ai-signal-formatter", "SEND_SOURCE_LINE=1"),
    @("exposedfx-ai-signal-formatter", "FORWARD_SIGNAL_CANDIDATES=1"),
    @("exposedfx-clean-signal-forwarder", "CLEAN_DELETE_SYNC_ENABLED=0"),
    @("exposedfx-clean-signal-forwarder", "FORWARD_CLEAN_UPDATES=0"),
    @("imperium-telegram-worker", "BLOCKED_SENDER_IDS=7556281143")
)
foreach ($v in $vars) {
    $service = $v[0]
    $setting = $v[1]
    Log "Setting $service $setting"
    railway.cmd variables --service "$service" --set "$setting" 2>&1 | Add-Content $OutputFile -Encoding UTF8
}

Run "DEPLOY AI FORMATTER" "railway.cmd" "up --service exposedfx-ai-signal-formatter --detach"

Start-Sleep -Seconds 45

Log ""
Log "==================== AI FORMATTER LOG CHECK ===================="
$AiLogs = railway.cmd logs --service "exposedfx-ai-signal-formatter" 2>&1
"AI sent count: $($AiLogs | Select-String -Pattern '\[signal hub sent\]|\[signal hub ai media sent\]' | Measure-Object | Select-Object -ExpandProperty Count)" | Add-Content $OutputFile -Encoding UTF8
"AI duplicate blocked count: $($AiLogs | Select-String -Pattern 'duplicate content signal|duplicate signal packet' | Measure-Object | Select-Object -ExpandProperty Count)" | Add-Content $OutputFile -Encoding UTF8
"AI errors count: $($AiLogs | Select-String -Pattern 'Traceback|SyntaxError|fatal crash|NameError|packet send failed|error|failed' | Measure-Object | Select-Object -ExpandProperty Count)" | Add-Content $OutputFile -Encoding UTF8
$AiLogs | Select-String -Pattern "Starting Container|Signal hub destination: -5252460120|CONTENT_DEDUPE_ENABLED=True|AUTO_TP_IF_MISSING|PURGE_DEST_ON_START=False|SEND_SOURCE_LINE|FORWARD_SIGNAL_CANDIDATES|signal hub sent|duplicate content signal|Traceback|SyntaxError|fatal crash|NameError|error|failed" | Select-Object -Last 180 | Add-Content $OutputFile -Encoding UTF8

Log ""
Log "==================== FINAL GIT STATUS ===================="
git status -sb 2>&1 | Add-Content $OutputFile -Encoding UTF8
git log --oneline -10 2>&1 | Add-Content $OutputFile -Encoding UTF8

Log ""
Log "DONE: report copied and opened. Paste it into ChatGPT."
Get-Content $OutputFile -Raw | Set-Clipboard
notepad $OutputFile
