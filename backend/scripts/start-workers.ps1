# Starts real OCR and business workers in the background; prints PIDs and log paths.
param(
    [ValidateRange(0, 32)][int]$OcrWorkers = 1,
    [ValidateRange(1, 32)][int]$BusinessWorkers = 1,
    [ValidateSet("cpu", "cuda", "mps", "auto")][string]$Device = "cpu",
    [string]$PythonExe
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$repo = Split-Path $root -Parent
if ($OcrWorkers -gt 0) {
    if (-not $PythonExe) { $PythonExe = Join-Path $repo "try1\.venv\Scripts\python.exe" }
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
    & $PythonExe -c "import pika, torch, PIL, ultralytics, transformers"
    if ($LASTEXITCODE -ne 0) { throw "Install dependencies first; see backend/README.md." }
}
dotnet build (Join-Path $root "OcrDemo.BusinessWorker") --nologo -v q
if ($LASTEXITCODE -ne 0) { throw "Business worker build failed." }
$logs = Join-Path $repo ".data\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:OCR_DEVICE = $Device
$env:DOTNET_ENVIRONMENT = "Development"
$run = Get-Date -Format "yyyyMMdd-HHmmss-fff"
for ($i = 1; $i -le $OcrWorkers; $i++) {
    $name = "ocr-$run-$i"
    $process = Start-Process -FilePath $PythonExe -ArgumentList "-u", "worker.py" -WorkingDirectory (Join-Path $root "ocr-worker") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "$name.out.log") -RedirectStandardError (Join-Path $logs "$name.err.log")
    Write-Host "OCR worker PID $($process.Id): $logs\$name.*.log"
}
for ($i = 1; $i -le $BusinessWorkers; $i++) {
    $name = "business-$run-$i"
    $dll = Join-Path $root "OcrDemo.BusinessWorker\bin\Debug\net10.0\OcrDemo.BusinessWorker.dll"
    $process = Start-Process -FilePath "dotnet" -ArgumentList @("`"$dll`"") -WorkingDirectory (Join-Path $root "OcrDemo.BusinessWorker") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "$name.out.log") -RedirectStandardError (Join-Path $logs "$name.err.log")
    Write-Host "Business worker PID $($process.Id): $logs\$name.*.log"
}
Write-Host "Workers run in the background. Stop an instance with taskkill /PID <PID> /T /F. Each OCR instance loads its own model; size worker counts for available RAM/VRAM."
