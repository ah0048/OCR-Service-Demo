# Starts N Python OCR workers and M .NET business workers as separate console windows.
# Usage: ./scripts/start-workers.ps1 -OcrWorkers 3 -BusinessWorkers 2 [-OcrDelaySeconds 2]
param(
    [int]$OcrWorkers = 1,
    [int]$BusinessWorkers = 1,
    [double]$OcrDelaySeconds = 2
)
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root "ocr-worker\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the venv first: see README (ocr-worker setup)." }

# Build once so the worker instances don't race each other on the build output.
dotnet build (Join-Path $root "OcrDemo.BusinessWorker") --nologo -v q | Out-Null

for ($i = 1; $i -le $OcrWorkers; $i++) {
    $cmd = "`$env:OCR_PROCESSING_DELAY_SECONDS='$OcrDelaySeconds'; `$host.UI.RawUI.WindowTitle='ocr-worker-$i'; & '$python' worker.py"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WorkingDirectory (Join-Path $root "ocr-worker")
}
for ($i = 1; $i -le $BusinessWorkers; $i++) {
    $cmd = "`$env:DOTNET_ENVIRONMENT='Development'; `$host.UI.RawUI.WindowTitle='business-worker-$i'; dotnet run --project OcrDemo.BusinessWorker --no-build"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WorkingDirectory $root
}
Write-Host "Started $OcrWorkers OCR worker(s) and $BusinessWorkers business worker(s). Close the windows (or Ctrl+C) to stop them."
