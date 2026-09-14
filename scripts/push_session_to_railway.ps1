# Pushes SESSION_B64_* variables from a vars file straight into a Railway service.
#
# The session key is never printed and never touched by hand: it is read from the
# file and handed to the Railway CLI as arguments.
#
# Sent in small batches on purpose. All 12 variables on one command line is about
# 38,500 characters, over the 32,767 Windows allows, so it would fail.
#
# Note on error handling: the Railway CLI writes notices such as
# "New version available" to stderr. PowerShell turns native stderr output into a
# NativeCommandError, so this script must NOT use $ErrorActionPreference = 'Stop'
# and must judge success by $LASTEXITCODE alone.
#
# Usage:
#   .\scripts\push_session_to_railway.ps1
#   .\scripts\push_session_to_railway.ps1 -VarsFile .\data\other.vars.txt -Service my-service

param(
    [string]$VarsFile = ".\data\clean_forwarder.vars.txt",
    [string]$Service  = "exposedfx-clean-signal-forwarder",
    [int]   $BatchSize = 3
)

$ErrorActionPreference = 'Continue'

function Invoke-Railway {
    param([string[]]$CliArgs)

    # 2>&1 folds stderr into the output stream so a notice cannot raise an error.
    $out = & railway @CliArgs 2>&1 | Out-String
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = $out
    }
}

if (-not (Test-Path $VarsFile)) {
    Write-Host "Missing $VarsFile" -ForegroundColor Red
    Write-Host "Generate it first with: python scripts\split_session_b64.py .\data\clean_forwarder.b64 11"
    exit 1
}

$lines = @(Get-Content $VarsFile | Where-Object { $_.Trim() -ne "" })

if ($lines.Count -eq 0) {
    Write-Host "$VarsFile is empty." -ForegroundColor Red
    exit 1
}

foreach ($line in $lines) {
    if ($line -notmatch '^[A-Za-z_][A-Za-z0-9_]*=') {
        Write-Host "Line does not look like KEY=VALUE - aborting without sending anything." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Service : $Service"
Write-Host "File    : $VarsFile"
Write-Host "Setting : $($lines.Count) variables in batches of $BatchSize"
Write-Host ""

# Probe once whether this CLI build knows --skip-deploys, so the redeploy can be
# held back until every chunk is in place.
$useSkip = $false
$probe = Invoke-Railway @('variables', '--help')
if ($probe.Output -match '--skip-deploys') {
    $useSkip = $true
    Write-Host "CLI supports --skip-deploys: holding the redeploy until all chunks are set." -ForegroundColor DarkGray
} else {
    Write-Host "CLI has no --skip-deploys: it may redeploy between batches, which is harmless." -ForegroundColor DarkGray
}
Write-Host ""

$batchIndex = 0
$total = [Math]::Ceiling($lines.Count / $BatchSize)

for ($i = 0; $i -lt $lines.Count; $i += $BatchSize) {
    $batchIndex++
    $end   = [Math]::Min($i + $BatchSize - 1, $lines.Count - 1)
    $batch = $lines[$i..$end]

    $names = ($batch | ForEach-Object { ($_ -split '=', 2)[0] }) -join ', '
    Write-Host "[$batchIndex/$total] $names" -NoNewline

    $cliArgs = @('variables', '--service', $Service)
    foreach ($line in $batch) {
        $cliArgs += '--set'
        $cliArgs += $line
    }
    if ($useSkip) { $cliArgs += '--skip-deploys' }

    $result = Invoke-Railway $cliArgs

    if ($result.ExitCode -ne 0) {
        Write-Host "  FAILED" -ForegroundColor Red
        Write-Host ""
        Write-Host "Railway CLI exit code $($result.ExitCode). Output:" -ForegroundColor Red

        # Print only lines that cannot contain a key: short ones, and never a
        # line carrying a long base64 run.
        foreach ($l in ($result.Output -split "`r?`n")) {
            if ($l.Length -gt 0 -and $l.Length -lt 200 -and $l -notmatch '[A-Za-z0-9+/]{100,}') {
                Write-Host "  $l"
            }
        }

        Write-Host ""
        Write-Host "Earlier batches were sent; later ones were not." -ForegroundColor Yellow
        Write-Host "Re-running this script is safe - it simply overwrites." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  ok" -ForegroundColor Green
}

Write-Host ""
Write-Host "All $($lines.Count) variables set on $Service." -ForegroundColor Green
Write-Host ""
Write-Host "Now redeploy:" -ForegroundColor Cyan
Write-Host "  railway redeploy --service $Service --yes"
Write-Host ""
Write-Host "Or click Deploy on the service in the Railway dashboard."
