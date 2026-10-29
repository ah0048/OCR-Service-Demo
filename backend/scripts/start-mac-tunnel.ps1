# Background forwarding with a dedicated, forwarding-only SSH key. No password is saved.
param(
    [string]$MacHost = '192.168.171.19',
    [string]$MacUser = 'entapp',
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
if ($MacHost -notmatch '^[a-zA-Z0-9.-]+$' -or $MacUser -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid SSH host or user.' }
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$state = Join-Path $repo '.data\mac-ssh'
$identity = Join-Path $state 'tunnel_ed25519_user'
$pidfile = Join-Path $state 'tunnel.pid'
$errorLog = Join-Path $state 'tunnel.err.log'
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$existing = $null
if (Test-Path -LiteralPath $pidfile) {
    $savedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidfile -Raw).Trim(), [ref]$savedPid)) {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid"
        if ($candidate -and $candidate.Name -eq 'ssh.exe' -and $candidate.CommandLine.Contains($identity) -and
            $candidate.CommandLine.Contains("$MacUser@$MacHost")) { $existing = $candidate }
    }
}
if ($Stop) {
    if ($existing) { Stop-Process -Id $existing.ProcessId; Write-Host "Stopped Mac SSH tunnel PID $($existing.ProcessId)" }
    else { Write-Host 'No managed Mac SSH tunnel is running.' }
    return
}
if ($existing) { Write-Host "Mac SSH tunnel already running: PID $($existing.ProcessId)"; return }
if (-not (Test-Path -LiteralPath $identity)) { throw "Missing forwarding-only SSH key: $identity. See backend/README.md." }
$arguments = @('-N', '-T', '-i', "`"$identity`"", '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    '-R', '127.0.0.1:15673:127.0.0.1:5672', '-R', '127.0.0.1:15125:127.0.0.1:5125', "$MacUser@$MacHost")
$process = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardError $errorLog -RedirectStandardOutput (Join-Path $state 'tunnel.out.log')
Start-Sleep -Seconds 2
if ($process.HasExited) {
    $details = Get-Content -LiteralPath $errorLog -Raw
    if ($details -match 'remote port forwarding failed') {
        throw "The Mac could not open its forwarding ports (15673/15125), usually because another or stale SSH tunnel owns them. In a Mac SSH terminal, inspect: lsof -nP -iTCP:15673 -iTCP:15125 -sTCP:LISTEN . Verify the existing tunnel before closing its specific user-owned process, then retry this launcher. SSH details: $details"
    }
    throw "SSH tunnel failed: $details"
}
Set-Content -LiteralPath $pidfile -Value $process.Id
Write-Host "Background Mac SSH tunnel started: PID $($process.Id). Both Windows services must remain running."
