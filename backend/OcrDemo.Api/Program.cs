using System.ComponentModel.DataAnnotations;
using Microsoft.EntityFrameworkCore;
using OcrDemo.Api;
using OcrDemo.Shared;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOcrDb(builder.Configuration);
builder.Services.AddSingleton<JobPublisher>();
// UseProxy=false: on machines with a system proxy configured, HttpClient would route
// localhost:15672 through it (the proxy refuses loopback requests with 400).
builder.Services.AddHttpClient<RabbitManagement>((sp, c) => RabbitManagement.Configure(c, sp.GetRequiredService<IConfiguration>()))
    .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { UseProxy = false });
builder.Services.AddSingleton<DashboardService>();
builder.Services.AddCors(o => o.AddDefaultPolicy(p => p
    .WithOrigins(builder.Configuration.GetSection("Cors:Origins").Get<string[]>() ?? ["http://localhost:4200"])
    .AllowAnyHeader().AllowAnyMethod()));
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();
builder.Services.AddHealthChecks().AddDbContextCheck<OcrDbContext>();

var app = builder.Build();

app.UseCors();

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

if (builder.Configuration.GetValue<bool>("Database:AutoMigrate"))
{
    using var scope = app.Services.CreateScope();
    await scope.ServiceProvider.GetRequiredService<OcrDbContext>().Database.MigrateAsync();
}

app.MapHealthChecks("/health");
app.MapDashboard();

static Job NewJob(string clientReference, string imageReference, Guid? batchId = null, string? batchName = null) => new()
{
    BatchId = batchId,
    BatchName = batchName,
    Id = Guid.NewGuid(),
    CorrelationId = Guid.NewGuid(),
    ClientReference = clientReference,
    ImageReference = imageReference,
    Status = JobStatus.Queued,
    CreatedAt = DateTime.UtcNow,
};

static OcrJobMessage ToMessage(Job j) => new(j.Id, j.CorrelationId, j.ClientReference, j.ImageReference);

// Save to DB first (durable), then publish. If publishing fails the job stays "Queued" in the DB
// (documented limitation: no outbox, so it is not re-published automatically).
async Task<IResult> Submit(IReadOnlyList<Job> jobs, OcrDbContext db, JobPublisher publisher, CancellationToken ct, Func<IResult> ok)
{
    db.Jobs.AddRange(jobs);
    await db.SaveChangesAsync(ct);
    try
    {
        await publisher.PublishAsync(jobs.Select(ToMessage), ct);
    }
    catch (Exception ex) when (ex is not OperationCanceledException)
    {
        app.Logger.LogError(ex, "Failed to publish {Count} job(s) to RabbitMQ; jobs remain Queued in DB", jobs.Count);
        return Results.Problem("Jobs were saved but could not be published to the queue.", statusCode: 503);
    }
    app.Logger.LogInformation("Accepted {Count} job(s)", jobs.Count);
    return ok();
}

app.MapPost("/api/jobs", async (SubmitJobRequest req, OcrDbContext db, JobPublisher publisher, CancellationToken ct) =>
{
    if (string.IsNullOrWhiteSpace(req.ClientReference) || string.IsNullOrWhiteSpace(req.ImageReference))
        return Results.ValidationProblem(new Dictionary<string, string[]> { ["request"] = ["clientReference and imageReference are required"] });
    var job = NewJob(req.ClientReference, req.ImageReference);
    return await Submit([job], db, publisher, ct,
        () => Results.Accepted($"/api/jobs/{job.Id}", new { jobId = job.Id, statusUrl = $"/api/jobs/{job.Id}" }));
});

app.MapPost("/api/jobs/batch", async (BatchRequest req, OcrDbContext db, JobPublisher publisher, IConfiguration config, CancellationToken ct) =>
{
    var max = config.GetValue("Batch:MaxSize", 10000);
    if (req.Count < 1 || req.Count > max)
        return Results.ValidationProblem(new Dictionary<string, string[]> { ["count"] = [$"count must be between 1 and {max}"] });
    var prefix = string.IsNullOrWhiteSpace(req.ClientReferencePrefix) ? "LOAD" : req.ClientReferencePrefix;
    var batchId = Guid.NewGuid();
    var jobs = Enumerable.Range(1, req.Count).Select(i => NewJob($"{prefix}-{i}", $"mock-id-{Guid.NewGuid():N}.jpg", batchId, prefix)).ToList();
    return await Submit(jobs, db, publisher, ct,
        () => Results.Accepted("/api/dashboard/snapshot", new { batchId, count = jobs.Count, firstJobId = jobs[0].Id }));
});

app.MapGet("/api/jobs/{id:guid}", async (Guid id, OcrDbContext db, CancellationToken ct) =>
    await db.Jobs.AsNoTracking().FirstOrDefaultAsync(j => j.Id == id, ct) is { } j
        ? Results.Ok(new
        {
            jobId = j.Id, j.CorrelationId, j.BatchId, j.BatchName, j.OcrWorkerId, j.BusinessWorkerId, j.ClientReference, j.ImageReference,
            status = j.Status.ToString(), outcome = j.Outcome?.ToString(), j.ReasonCodes,
            ocr = j.OcrOutcome is null ? null : new { outcome = j.OcrOutcome, j.Confidence, durationMs = j.OcrDurationMs, j.ModelVersion, fields = j.ExtractedFields },
            j.CreatedAt, j.StartedAt, j.CompletedAt, j.ResultDeliveries,
        })
        : Results.NotFound());

// Aggregate counts, handy for the load test.
app.MapGet("/api/stats", async (OcrDbContext db, CancellationToken ct) =>
{
    var byStatus = await db.Jobs.GroupBy(j => j.Status).Select(g => new { k = g.Key.ToString(), n = g.Count() }).ToListAsync(ct);
    var byOutcome = await db.Jobs.Where(j => j.Outcome != null).GroupBy(j => j.Outcome).Select(g => new { k = g.Key.ToString(), n = g.Count() }).ToListAsync(ct);
    var first = await db.Jobs.MinAsync(j => (DateTime?)j.CreatedAt, ct);
    var last = await db.Jobs.MaxAsync(j => (DateTime?)j.CompletedAt, ct);
    return Results.Ok(new
    {
        total = byStatus.Sum(x => x.n),
        byStatus = byStatus.ToDictionary(x => x.k, x => x.n),
        byOutcome = byOutcome.ToDictionary(x => x.k!, x => x.n),
        firstCreatedAt = first,
        lastCompletedAt = last,
    });
});

app.Run();

record SubmitJobRequest(string ClientReference, string ImageReference);
record BatchRequest(int Count, string? ClientReferencePrefix);
