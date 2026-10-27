# Requeues a previously uploaded real image pair. Each job performs real OCR.
param(
    [Parameter(Mandatory = $true)][string]$ImageReference,
    [ValidateRange(1, 10000)][int]$Count = 10,
    [string]$ApiUrl = "http://localhost:5125"
)
$jobs = @(1..$Count | ForEach-Object { @{ clientReference = "LOAD-$_"; imageReference = $ImageReference } })
$sw = [Diagnostics.Stopwatch]::StartNew()
$resp = Invoke-WebRequest -Method Post "$ApiUrl/api/jobs/batch" -ContentType "application/json" -Body (@{ jobs = $jobs; batchName = "LOAD" } | ConvertTo-Json -Depth 4) -UseBasicParsing
$batch = $resp.Content | ConvertFrom-Json
Write-Host "HTTP $($resp.StatusCode): $Count jobs accepted in $($sw.ElapsedMilliseconds) ms."
$sw.Restart()
do {
    $page = Invoke-RestMethod "$ApiUrl/api/jobs?batchId=$($batch.batchId)&status=Completed&pageSize=1"
    Write-Host "Completed $($page.total)/$Count in $([int]$sw.Elapsed.TotalSeconds)s"
    if ($page.total -lt $Count) { Start-Sleep 3 }
} while ($page.total -lt $Count)
