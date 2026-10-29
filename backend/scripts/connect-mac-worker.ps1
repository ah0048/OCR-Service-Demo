# Start background forwarding, then start/reuse the Mac worker and follow its log.
param(
    [string]$MacHost = '192.168.171.19',
    [string]$MacUser = 'entapp',
    [string]$MacProject = '/Users/entapp/ocr-service-demo-for-mac/ocr_demo',
    [ValidateRange(1, 32)][int]$OcrWorkers = 1
)
$ErrorActionPreference = 'Stop'
if ($MacHost -notmatch '^[a-zA-Z0-9.-]+$' -or $MacUser -notmatch '^[a-zA-Z0-9_-]+$' -or
    $MacProject -notmatch '^/[a-zA-Z0-9_./ -]+$') { throw 'Invalid SSH host, user or project path.' }
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
if (-not (Test-Path -LiteralPath $ssh)) { throw 'Windows OpenSSH Client is required.' }
Write-Host 'Enter the Mac SSH password when prompted. No password is saved.'
& (Join-Path $PSScriptRoot 'start-mac-tunnel.ps1') -MacHost $MacHost -MacUser $MacUser
Write-Host "Starting/reusing $OcrWorkers Mac OCR workers. Ctrl+C closes log viewing; the background tunnel and workers continue running."
$remoteCommand = "bash '$MacProject/backend/scripts/mac-worker.sh' start $OcrWorkers && exec bash '$MacProject/backend/scripts/mac-worker.sh' logs"
& $ssh -T -o ServerAliveInterval=30 -o ServerAliveCountMax=3 "$MacUser@$MacHost" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'SSH log connection ended. The background tunnel and worker can be checked separately.' }
