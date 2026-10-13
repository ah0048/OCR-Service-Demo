# Submits a batch of mock jobs, shows how fast the API accepts them, then polls until all are completed.
# Usage: ./scripts/load-test.ps1 -Count 1000 [-ApiUrl http://localhost:5125]
param(
    [int]$Count = 100,
    [string]$ApiUrl = "http://localhost:5125"
)
$before = (Invoke-RestMethod "$ApiUrl/api/stats").byStatus.Completed
if (-not $before) { $before = 0 }

$sw = [Diagnostics.Stopwatch]::StartNew()
$resp = Invoke-WebRequest -Method Post "$ApiUrl/api/jobs/batch" -ContentType "application/json" `
    -Body (@{ count = $Count; clientReferencePrefix = "LOAD" } | ConvertTo-Json) -UseBasicParsing
$submitMs = $sw.ElapsedMilliseconds
Write-Host ("HTTP {0}: {1} jobs accepted in {2} ms (API did not wait for OCR)" -f $resp.StatusCode, $Count, $submitMs)

$sw.Restart()
while ($true) {
    $s = Invoke-RestMethod "$ApiUrl/api/stats"
    $done = [int]$s.byStatus.Completed - $before
    Write-Host ("  completed {0}/{1}  queued {2}  ({3:n0}s)" -f $done, $Count, [int]$s.byStatus.Queued, $sw.Elapsed.TotalSeconds)
    if ($done -ge $Count) { break }
    Start-Sleep 3
}
Write-Host ("All {0} jobs completed in {1:n1}s = {2:n1} jobs/s" -f $Count, $sw.Elapsed.TotalSeconds, ($Count / $sw.Elapsed.TotalSeconds))
$s.byOutcome | ConvertTo-Json -Compress
