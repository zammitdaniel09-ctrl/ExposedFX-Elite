$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# Always run from repo root, even if launched from another folder.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$OutputFile = Join-Path $env:TEMP "EXPOSEDFX_RECOVER_AND_DEPLOY_AI.txt"
$StepOutput = Join-Path $env:TEMP "EXPOSEDFX_STEP_OUTPUT.txt"

function Write-Log([string]$Text) {
    $Text | Out-File -FilePath $OutputFile -Append -Encoding utf8
    Write-Host $Text
}

function Stop-WithReport([string]$Message, [int]$Code = 1) {
    Write-Log ""
    Write-Log "FAILED: $Message"
    if (Test-Path $StepOutput) {
        Write-Log ""
        Write-Log "LAST STEP OUTPUT:"
        Get-Content $StepOutput -Raw | Out-File -FilePath $OutputFile -Append -Encoding utf8
    }
    Get-Content $OutputFile -Raw | Set-Clipboard
    notepad $OutputFile
    exit $Code
}

function Run-Command([string]$Name, [string]$CommandLine) {
    Write-Log ""
    Write-Log "==================== $Name ===================="
    Write-Log "COMMAND: $CommandLine"
    if (Test-Path $StepOutput) { Remove-Item $StepOutput -Force -ErrorAction SilentlyContinue }

    cmd.exe /d /c "$CommandLine" > $StepOutput 2>&1
    $Exit = $LASTEXITCODE

    if (Test-Path $StepOutput) {
        Get-Content $StepOutput -Raw | Out-File -FilePath $OutputFile -Append -Encoding utf8
        Get-Content $StepOutput -Raw | Write-Host
    }

    if ($Exit -ne 0) {
        Stop-WithReport "$Name failed with exit code $Exit" $Exit
    }
}

# Make sure fresh installs are visible in this PowerShell session.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + "$env:APPDATA\npm"

"==================== EXPOSEDFX RECOVER AND DEPLOY AI ====================" | Set-Content $OutputFile -Encoding utf8
"Time: $(Get-Date)" | Out-File -FilePath $OutputFile -Append -Encoding utf8
"Folder: $(Get-Location)" | Out-File -FilePath $OutputFile -Append -Encoding utf8

Write-Host "==================== EXPOSEDFX RECOVER AND DEPLOY AI ===================="
Write-Host "Time: $(Get-Date)"
Write-Host "Folder: $(Get-Location)"

Write-Log ""
Write-Log "==================== POWERSHELL READY CHECK ===================="
Write-Log "PowerShell is executing scripts correctly."
Write-Log "If you can read this, the prompt is no longer stuck in >> mode."
Write-Log "PSVersion: $($PSVersionTable.PSVersion)"
Write-Log "User: $env:USERNAME"

Run-Command "CHECK GIT" "where.exe git && git --version"
Run-Command "CHECK PYTHON" "where.exe python && python --version"
Run-Command "CHECK RAILWAY" "where.exe railway.cmd && railway.cmd --version"

Write-Log ""
Write-Log "==================== GIT SYNC ===================="
git status -sb 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git fetch origin 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git reset --hard origin/main 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git pull origin main 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git status -sb 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git log --oneline -10 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8

git status -sb
git log --oneline -5

Write-Log ""
Write-Log "==================== VERIFY REQUIRED PATCHES ===================="
$Checks = @(
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

$Missing = 0
foreach ($Check in $Checks) {
    $Hit = Select-String -Path $Check.File -Pattern $Check.Pattern -SimpleMatch -ErrorAction SilentlyContinue
    if ($Hit) {
        Write-Log "OK: $($Check.File) contains $($Check.Pattern)"
    } else {
        Write-Log "MISSING: $($Check.File) lacks $($Check.Pattern)"
        $Missing++
    }
}

if ($Missing -gt 0) {
    Stop-WithReport "Required code patches are missing. Do not deploy." 10
}

Run-Command "PYTHON COMPILE" "python -m py_compile telegram_worker\routes.py telegram_worker\worker_fixed.py telegram_worker\worker_signal_hub.py telegram_worker\universal_signal_ai.py telegram_worker\signal_refiner.py telegram_worker\worker_clean_signal_forwarder.py"

Write-Log ""
Write-Log "==================== RAILWAY STATUS BEFORE ===================="
railway.cmd status 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
railway.cmd status

Write-Log ""
Write-Log "==================== SET SAFE VARIABLES ===================="
$Vars = @(
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

foreach ($Var in $Vars) {
    $Service = $Var[0]
    $Setting = $Var[1]
    Write-Log "Setting $Service $Setting"
    railway.cmd variables --service "$Service" --set "$Setting" 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
}

Run-Command "DEPLOY AI FORMATTER" "railway.cmd up --service exposedfx-ai-signal-formatter --detach"

Write-Log "Waiting 45 seconds for deployment logs..."
Start-Sleep -Seconds 45

Write-Log ""
Write-Log "==================== AI FORMATTER LOG CHECK ===================="
$AiLogs = railway.cmd logs --service "exposedfx-ai-signal-formatter" 2>&1
"AI sent count: $($AiLogs | Select-String -Pattern '\[signal hub sent\]|\[signal hub ai media sent\]' | Measure-Object | Select-Object -ExpandProperty Count)" | Out-File -FilePath $OutputFile -Append -Encoding utf8
"AI duplicate blocked count: $($AiLogs | Select-String -Pattern 'duplicate content signal|duplicate signal packet' | Measure-Object | Select-Object -ExpandProperty Count)" | Out-File -FilePath $OutputFile -Append -Encoding utf8
"AI errors count: $($AiLogs | Select-String -Pattern 'Traceback|SyntaxError|fatal crash|NameError|packet send failed|error|failed' | Measure-Object | Select-Object -ExpandProperty Count)" | Out-File -FilePath $OutputFile -Append -Encoding utf8
$AiLogs | Select-String -Pattern "Starting Container|Signal hub destination: -5252460120|CONTENT_DEDUPE_ENABLED=True|AUTO_TP_IF_MISSING|PURGE_DEST_ON_START=False|SEND_SOURCE_LINE|FORWARD_SIGNAL_CANDIDATES|signal hub sent|duplicate content signal|Traceback|SyntaxError|fatal crash|NameError|error|failed" | Select-Object -Last 180 | Out-File -FilePath $OutputFile -Append -Encoding utf8

Write-Log ""
Write-Log "==================== FINAL GIT STATUS ===================="
git status -sb 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8
git log --oneline -10 2>&1 | Out-File -FilePath $OutputFile -Append -Encoding utf8

Write-Log ""
Write-Log "DONE: report copied and opened. Paste it into ChatGPT."
Get-Content $OutputFile -Raw | Set-Clipboard
notepad $OutputFile
