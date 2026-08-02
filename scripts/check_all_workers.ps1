$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User") + ";$env:APPDATA\npm"

$OutputFile = Join-Path $env:TEMP "EXPOSEDFX_ALL_WORKERS_CHECK.txt"
Remove-Item $OutputFile -Force -ErrorAction SilentlyContinue

$Services = @(
    "imperium-telegram-worker",
    "exposedfx-ai-signal-formatter",
    "exposedfx-clean-signal-forwarder",
    "ExposedFX-Elite"
)

$Pattern = "Logged in as|running|started|Allowed topics|PURGE_DEST_ON_START|signal hub seen|signal packet mapped|new mirror poll init|new mirror poll copied|clean|Traceback|ERROR|fatal|crash|AuthKeyDuplicated|duplicate local worker|not authorised|unauthorised|SessionPasswordNeeded|PasswordHashInvalid|FloodWait|failed|exception"

"EXPOSEDFX ALL WORKERS CHECK" | Set-Content $OutputFile -Encoding UTF8
"Time: $(Get-Date)" | Add-Content $OutputFile -Encoding UTF8
"Commit: $(git rev-parse --short HEAD 2>$null)" | Add-Content $OutputFile -Encoding UTF8

foreach ($Service in $Services) {
    $Header = "`r`n==================== $Service ===================="
    Write-Host $Header
    $Header | Add-Content $OutputFile -Encoding UTF8

    $Raw = & railway.cmd logs --service $Service 2>&1
    $ExitCode = $LASTEXITCODE

    if ($Raw) {
        $Filtered = $Raw | Select-String -Pattern $Pattern | Select-Object -Last 250
        if ($Filtered) {
            $Filtered | Tee-Object -FilePath $OutputFile -Append
        } else {
            "No matching diagnostic lines. Last 80 raw lines:" | Tee-Object -FilePath $OutputFile -Append
            $Raw | Select-Object -Last 80 | Tee-Object -FilePath $OutputFile -Append
        }
    }

    "Railway logs exit code: $ExitCode" | Tee-Object -FilePath $OutputFile -Append
}

Write-Host ""
Write-Host "Diagnostic report: $OutputFile"
Write-Host "The report has been copied to your clipboard and opened in Notepad."
Get-Content $OutputFile -Raw | Set-Clipboard
notepad $OutputFile
