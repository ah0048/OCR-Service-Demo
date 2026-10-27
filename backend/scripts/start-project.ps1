# Invoked by start.cmd from any working directory.
[CmdletBinding()]
param(
    [ValidateRange(1, 32)][int]$OcrWorkers = 3,
    [ValidateRange(1, 32)][int]$BusinessWorkers = 2,
    [ValidateSet("cpu", "cuda", "mps", "auto")][string]$Device = "cpu",
    [string]$PythonExe
)
$ErrorActionPreference = "Stop"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location -LiteralPath $repo

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit $LASTEXITCODE)." }
}

function Test-Http {
    param([string]$Url)
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Proxy = $null
        $request.Timeout = 2000
        $response = $request.GetResponse()
        return ([int]$response.StatusCode -eq 200)
    } catch { return $false }
    finally { if ($response) { $response.Close() } }
}

function Wait-Http {
    param([string]$Url, [System.Diagnostics.Process]$Process, [string]$LogPrefix)
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline) {
        if (Test-Http $Url) { return }
        if ($Process.HasExited) { throw "Service exited. See $LogPrefix.out.log and $LogPrefix.err.log." }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for $Url. See $LogPrefix.out.log and $LogPrefix.err.log."
}

try {
    foreach ($command in @("docker", "dotnet", "node", "npm.cmd")) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Missing prerequisite: $command. Install Docker Desktop, .NET 10 SDK and Node.js/npm before running start.cmd."
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repo "try1\pipeline\document.py"))) {
        throw "The real OCR project is missing. Place it, including its offline model assets, in try1."
    }

    Write-Host "[1/5] Preparing Python dependencies..."
    if (-not $PythonExe) {
        $PythonExe = Join-Path $repo "try1\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            $PythonExe = Join-Path $repo "backend\ocr-worker\.venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $PythonExe)) {
                $venv = Join-Path $repo "backend\ocr-worker\.venv"
                if (Get-Command py -ErrorAction SilentlyContinue) {
                    Invoke-Checked "py" @("-3", "-m", "venv", $venv)
                } elseif (Get-Command python -ErrorAction SilentlyContinue) {
                    Invoke-Checked "python" @("-m", "venv", $venv)
                } else { throw "Install Python 3.11+ or pass -PythonExe with an existing OCR environment." }
            }
        }
    }
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
    & $PythonExe -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(n) for n in ('torch', 'torchvision', 'PIL', 'ultralytics', 'transformers', 'accelerate', 'cv2', 'numpy', 'safetensors', 'qwen_vl_utils')) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Invoke-Checked $PythonExe @("-m", "pip", "install", "-r", (Join-Path $repo "backend\ocr-worker\requirements.real.txt"))
    } else {
        & $PythonExe -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pika') else 1)"
        if ($LASTEXITCODE -ne 0) {
            Invoke-Checked $PythonExe @("-m", "pip", "install", "-r", (Join-Path $repo "backend\ocr-worker\requirements.txt"))
        }
    }

    Write-Host "[2/5] Preparing frontend dependencies..."
    $frontend = Join-Path $repo "frontend"
    if (-not (Test-Path -LiteralPath (Join-Path $frontend "node_modules\.bin\ng.cmd"))) {
        Invoke-Checked "npm.cmd" @("--prefix", $frontend, "ci")
    }

    Write-Host "[3/5] Starting PostgreSQL and RabbitMQ (Docker Desktop must be running)..."
    Invoke-Checked "docker" @("compose", "-f", (Join-Path $repo "backend\docker-compose.yml"), "up", "-d", "--wait", "--wait-timeout", "180")

    $logs = Join-Path $repo ".data\logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $run = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $env:DOTNET_ENVIRONMENT = "Development"
    $env:ASPNETCORE_ENVIRONMENT = "Development"
    Write-Host "[4/5] Starting API and workers..."
    if (Test-Http "http://localhost:5125/health") {
        Write-Host "API already running on port 5125; using it."
    } else {
        $api = Join-Path $repo "backend\OcrDemo.Api"
        Invoke-Checked "dotnet" @("build", $api, "--nologo", "-v", "q")
        $dll = Join-Path $api "bin\Debug\net10.0\OcrDemo.Api.dll"
        $prefix = Join-Path $logs "api-$run"
        $process = Start-Process -FilePath "dotnet" -ArgumentList @("`"$dll`"", "--urls", "http://localhost:5125") -WorkingDirectory $api -WindowStyle Hidden -PassThru -RedirectStandardOutput "$prefix.out.log" -RedirectStandardError "$prefix.err.log"
        Write-Host "API PID $($process.Id): $prefix.*.log"
        # Development applies DB migrations before business workers start.
        Wait-Http "http://localhost:5125/health" $process $prefix
    }
    & (Join-Path $PSScriptRoot "start-workers.ps1") -OcrWorkers $OcrWorkers -BusinessWorkers $BusinessWorkers -Device $Device -PythonExe $PythonExe

    Write-Host "[5/5] Starting frontend..."
    if (Test-Http "http://localhost:4200") {
        Write-Host "Frontend already running on port 4200; using it."
    } else {
        $prefix = Join-Path $logs "frontend-$run"
        $process = Start-Process -FilePath $env:ComSpec -ArgumentList @("/d", "/c", "npm.cmd start -- --host localhost --port 4200") -WorkingDirectory $frontend -WindowStyle Hidden -PassThru -RedirectStandardOutput "$prefix.out.log" -RedirectStandardError "$prefix.err.log"
        Write-Host "Frontend PID $($process.Id): $prefix.*.log"
        Wait-Http "http://localhost:4200" $process $prefix
    }
    Write-Host ""
    Write-Host "Frontend: http://localhost:4200"
    Write-Host "API:      http://localhost:5125"
    Write-Host "RabbitMQ: http://localhost:15672"
    Write-Host "Started $OcrWorkers OCR workers and $BusinessWorkers business workers (device: $Device)."
    Write-Host "OCR models load in the background; check the worker logs for readiness."
    Write-Host "Logs: $logs"
    Write-Host "Processes continue after this window closes. Stop a printed PID with taskkill /PID <PID> /T /F."
    Write-Host "Running start.cmd again adds another set of workers."
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Any services already started remain running; their PIDs and logs are printed above."
    exit 1
}
