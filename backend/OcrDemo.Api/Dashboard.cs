using System.Text.Json;
using Microsoft.EntityFrameworkCore;
using OcrDemo.Shared;

namespace OcrDemo.Api;

/// <summary>Builds the one-second snapshot the dashboard renders: totals, throughput, queues, workers and batches.</summary>
public sealed class DashboardService(IServiceScopeFactory scopes, RabbitManagement rabbit)
{
    public async Task<object> BuildAsync(CancellationToken ct)
    {
        var rabbitTask = rabbit.GetAsync(ct);

        using var scope = scopes.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<OcrDbContext>();
        var now = DateTime.UtcNow;

        var byStatus = await db.Jobs.GroupBy(j => j.Status).Select(g => new { g.Key, N = g.Count() }).ToListAsync(ct);
        var byOutcome = await db.Jobs.Where(j => j.Outcome != null).GroupBy(j => j.Outcome).Select(g => new { g.Key, N = g.Count() }).ToListAsync(ct);
        int Status(JobStatus s) => byStatus.FirstOrDefault(x => x.Key == s)?.N ?? 0;

        var last10 = await db.Jobs.CountAsync(j => j.CompletedAt > now.AddSeconds(-10), ct);
        var last60 = await db.Jobs.CountAsync(j => j.CompletedAt > now.AddSeconds(-60), ct);

        var recent = await db.Jobs.AsNoTracking().Where(j => j.Status == JobStatus.Completed && j.CompletedAt > now.AddMinutes(-5)) // recent window, so old backlog doesn't skew the numbers
            .OrderByDescending(j => j.CompletedAt).Take(200)
            .Select(j => new { j.CreatedAt, j.CompletedAt, j.OcrDurationMs }).ToListAsync(ct);
        var endToEnd = recent.Select(j => (j.CompletedAt!.Value - j.CreatedAt).TotalMilliseconds).OrderBy(x => x).ToList();

        var batches = await db.Jobs.Where(j => j.BatchId != null)
            .GroupBy(j => j.BatchId)
            .Select(g => new
            {
                Id = g.Key,
                Name = g.Max(j => j.BatchName),
                Total = g.Count(),
                Queued = g.Count(j => j.Status == JobStatus.Queued),
                InProgress = g.Count(j => j.Status == JobStatus.OcrInProgress),
                Succeeded = g.Count(j => j.Outcome == JobOutcome.Succeeded),
                ValidationFailed = g.Count(j => j.Outcome == JobOutcome.ValidationFailed),
                ManualReview = g.Count(j => j.Outcome == JobOutcome.ManualReview),
                OcrFailed = g.Count(j => j.Outcome == JobOutcome.OcrFailed),
                CreatedAt = g.Min(j => j.CreatedAt),
                LastCompletedAt = g.Max(j => j.CompletedAt),
            })
            .OrderByDescending(b => b.CreatedAt).Take(8).ToListAsync(ct);

        var ocrCounts = await db.Jobs.Where(j => j.OcrWorkerId != null).GroupBy(j => j.OcrWorkerId)
            .Select(g => new { Id = g.Key!, N = g.Count(), Last = g.Max(j => j.StartedAt) }).ToListAsync(ct);
        var bizCounts = await db.Jobs.Where(j => j.BusinessWorkerId != null).GroupBy(j => j.BusinessWorkerId)
            .Select(g => new { Id = g.Key!, N = g.Count(), Last = g.Max(j => j.CompletedAt) }).ToListAsync(ct);

        var snap = await rabbitTask;
        object Workers(string queue, string kind, IEnumerable<(string Id, int N, DateTime? Last)> history)
        {
            var online = snap?.Consumers.Where(c => c.Queue == queue).ToList() ?? [];
            var list = online.Select(c =>
                {
                    var h = history.FirstOrDefault(x => x.Id == c.Connection);
                    return new { id = c.Connection, online = true, busy = c.ChannelUnacked > 0, prefetch = c.Prefetch, processed = h.N, lastActivity = h.Last };
                })
                .Concat(history.Where(h => online.All(c => c.Connection != h.Id))
                    .Select(h => new { id = h.Id, online = false, busy = false, prefetch = 0, processed = h.N, lastActivity = h.Last }))
                .OrderByDescending(w => w.online).ThenBy(w => w.id).ToList();
            return new { kind, onlineCount = online.Count, workers = list };
        }

        double Pct(double p) => endToEnd.Count == 0 ? 0 : endToEnd[Math.Min(endToEnd.Count - 1, (int)Math.Ceiling(p * endToEnd.Count) - 1)];

        return new
        {
            serverTime = now,
            rabbitAvailable = snap is not null,
            totals = new
            {
                total = byStatus.Sum(x => x.N),
                queued = Status(JobStatus.Queued),
                inProgress = Status(JobStatus.OcrInProgress),
                completed = Status(JobStatus.Completed),
                byOutcome = byOutcome.ToDictionary(x => x.Key.ToString()!, x => x.N),
            },
            throughput = new { perSecond10s = Math.Round(last10 / 10.0, 2), perSecond60s = Math.Round(last60 / 60.0, 2) },
            latency = new
            {
                sampleSize = endToEnd.Count,
                avgEndToEndMs = endToEnd.Count == 0 ? 0 : Math.Round(endToEnd.Average()),
                p95EndToEndMs = Math.Round(Pct(0.95)),
                avgOcrMs = recent.Count == 0 ? 0 : Math.Round(recent.Average(j => j.OcrDurationMs ?? 0.0)),
            },
            queues = snap?.Queues ?? [],
            ocrWorkers = Workers(Queues.OcrJobs, "ocr", ocrCounts.Select(x => (x.Id, x.N, x.Last))),
            businessWorkers = Workers(Queues.OcrResults, "business", bizCounts.Select(x => (x.Id, x.N, x.Last))),
            batches = batches.Select(b => new
            {
                id = b.Id, name = b.Name, b.Total, b.Queued, b.InProgress,
                completed = b.Succeeded + b.ValidationFailed + b.ManualReview + b.OcrFailed,
                b.Succeeded, b.ValidationFailed, b.ManualReview, b.OcrFailed, b.CreatedAt, b.LastCompletedAt,
            }),
        };
    }
}

public static class DashboardEndpoints
{
    public static void MapDashboard(this WebApplication app)
    {
        // Server-sent events: one JSON snapshot per interval. EventSource reconnects automatically.
        app.MapGet("/api/dashboard/stream", async (HttpContext ctx, DashboardService svc, IConfiguration config, ILogger<DashboardService> log, CancellationToken ct) =>
        {
            ctx.Response.Headers.ContentType = "text/event-stream";
            ctx.Response.Headers.CacheControl = "no-cache";
            var interval = TimeSpan.FromMilliseconds(config.GetValue("Dashboard:IntervalMs", 1000));
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    try
                    {
                        var json = JsonSerializer.Serialize(await svc.BuildAsync(ct), Json.Options);
                        await ctx.Response.WriteAsync($"data: {json}\n\n", ct);
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException)
                    {
                        log.LogError(ex, "Failed to build dashboard snapshot");
                        await ctx.Response.WriteAsync(": snapshot failed\n\n", ct); // comment keeps the connection alive
                    }
                    await ctx.Response.Body.FlushAsync(ct);
                    await Task.Delay(interval, ct);
                }
            }
            catch (OperationCanceledException) { /* client disconnected */ }
        });

        app.MapGet("/api/dashboard/snapshot", async (DashboardService svc, CancellationToken ct) => Results.Ok(await svc.BuildAsync(ct)));

        // Pushes each job's OCR result the moment the job completes: first the latest `backlog` results
        // (oldest first), then every new one. One SSE message per result.
        app.MapGet("/api/results/stream", async (HttpContext ctx, IServiceScopeFactory scopes, ILogger<DashboardService> log, CancellationToken ct, int backlog = 30) =>
        {
            ctx.Response.Headers.ContentType = "text/event-stream";
            ctx.Response.Headers.CacheControl = "no-cache";
            backlog = Math.Clamp(backlog, 0, 200);

            // Commits can land slightly out of timestamp order (several business workers), so each poll looks a few
            // seconds behind the newest result seen and de-duplicates by job id instead of trusting the cursor alone.
            var sent = new Dictionary<Guid, DateTime>();
            var cursor = DateTime.UtcNow.AddMinutes(-1);
            var first = true;
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    try
                    {
                        using var scope = scopes.CreateScope();
                        var db = scope.ServiceProvider.GetRequiredService<OcrDbContext>();
                        var q = db.Jobs.AsNoTracking().Where(j => j.Status == JobStatus.Completed && j.OcrOutcome != null);
                        var rows = first
                            ? (await q.OrderByDescending(j => j.CompletedAt).Take(backlog).ToListAsync(ct)).AsEnumerable().Reverse().ToList()
                            : await q.Where(j => j.CompletedAt > cursor.AddSeconds(-5)).OrderBy(j => j.CompletedAt).Take(200).ToListAsync(ct);
                        var eventName = first ? "event: backlog\n" : ""; // live results use the default "message" event
                        first = false;

                        foreach (var j in rows.Where(j => !sent.ContainsKey(j.Id)))
                        {
                            sent[j.Id] = j.CompletedAt!.Value;
                            if (j.CompletedAt > cursor) cursor = j.CompletedAt.Value;
                            var json = JsonSerializer.Serialize(new
                            {
                                jobId = j.Id, j.ClientReference, j.ImageReference, j.BatchName,
                                outcome = j.Outcome?.ToString(), j.ReasonCodes,
                                ocr = new { outcome = j.OcrOutcome, j.Confidence, durationMs = j.OcrDurationMs, j.ModelVersion, fields = j.ExtractedFields },
                                j.OcrWorkerId, j.BusinessWorkerId, j.CreatedAt, j.StartedAt, j.CompletedAt,
                            }, Json.Options);
                            await ctx.Response.WriteAsync($"{eventName}data: {json}\n\n", ct);
                        }
                        foreach (var old in sent.Where(kv => kv.Value < cursor.AddSeconds(-30)).Select(kv => kv.Key).ToList()) sent.Remove(old);
                        await ctx.Response.WriteAsync(": keep-alive\n\n", ct);
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException)
                    {
                        log.LogError(ex, "Results stream poll failed");
                    }
                    await ctx.Response.Body.FlushAsync(ct);
                    await Task.Delay(500, ct);
                }
            }
            catch (OperationCanceledException) { /* client disconnected */ }
        });

        // Paged, filterable jobs table.
        app.MapGet("/api/jobs", async (OcrDbContext db, CancellationToken ct,
            int page = 1, int pageSize = 25, string? status = null, string? outcome = null, Guid? batchId = null, string? search = null) =>
        {
            page = Math.Max(1, page);
            pageSize = Math.Clamp(pageSize, 1, 200);
            var q = db.Jobs.AsNoTracking().AsQueryable();
            if (Enum.TryParse<JobStatus>(status, true, out var st)) q = q.Where(j => j.Status == st);
            if (Enum.TryParse<JobOutcome>(outcome, true, out var oc)) q = q.Where(j => j.Outcome == oc);
            if (batchId is { } b) q = q.Where(j => j.BatchId == b);
            if (!string.IsNullOrWhiteSpace(search))
            {
                var s = search.Trim();
                q = Guid.TryParse(s, out var id)
                    ? q.Where(j => j.Id == id)
                    : q.Where(j => EF.Functions.ILike(j.ClientReference, $"%{s}%") || EF.Functions.ILike(j.ImageReference, $"%{s}%"));
            }

            var total = await q.CountAsync(ct);
            var items = await q.OrderByDescending(j => j.CreatedAt).ThenBy(j => j.Id).Skip((page - 1) * pageSize).Take(pageSize).ToListAsync(ct);
            return Results.Ok(new
            {
                total, page, pageSize,
                items = items.Select(j => new
                {
                    jobId = j.Id, j.BatchId, j.BatchName, j.ClientReference, j.ImageReference,
                    status = j.Status.ToString(), outcome = j.Outcome?.ToString(), j.ReasonCodes,
                    j.OcrOutcome, j.Confidence, j.OcrDurationMs, j.OcrWorkerId, j.BusinessWorkerId,
                    j.CreatedAt, j.StartedAt, j.CompletedAt, j.ResultDeliveries,
                }),
            });
        });
    }
}
