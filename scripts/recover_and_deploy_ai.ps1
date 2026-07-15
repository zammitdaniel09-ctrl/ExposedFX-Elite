$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$OutputFile = Join-Path $env:TEMP "EXPOSEDFX_RECOVER_AND_DEPLOY_AI.txt"
"==================== EXPOSEDFX RECOVER AND DEPLOY AI ====================" | Set-Content $OutputFile -Encoding UTF8
"Time: $(Get-Date)" | Add-Content $OutputFile -Encoding UTF8
"Folder: $(Get-Location)" | Add-Content $OutputFile -Encoding UTF8

function Log($Text) {
    $Text | Tee