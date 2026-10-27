using System.ComponentModel.DataAnnotations;
using Microsoft.EntityFrameworkCore;
using OcrDemo.Api;
using OcrDemo.Shared;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOcrDb(builder.Configuration);
builder.Services.AddSingleton<JobPublisher>();
builder.Services.AddSingleton<ImageStore>();
builder.WebHost.ConfigureKestrel(o => o.Limits.MaxRequestBodySize = ImageStore.MaxUploadBytes);
builder.Services.Configure<Microsoft.AspNetCore.Http.Features.FormOptions>(o =>
{
    o.MultipartBodyLengthLimit = ImageStore.MaxUploadBytes;
    o.ValueCountLimit = 1000;
});
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

app.MapPost("/api/jobs", async (SubmitJobRequest req, OcrDbContext db, JobPublisher publisher, ImageStore images, CancellationToken ct) =>
{
    if (string.IsNullOrWhiteSpace(req.ClientReference) || string.IsNullOrWhiteSpace(req.ImageReference) ||
        images.PairDirectory(req.ImageReference) is null)
        return Results.ValidationProblem(new Dictionary<string, string[]> { ["request"] = ["clientReference and an existing uploaded imageReference are required; upload front/back files at /api/jobs/upload"] });
    var job = NewJob(req.ClientReference, req.ImageReference);
    return await Submit([job], db, publisher, ct,
        () => Results.Accepted($"/api/jobs/{job.Id}", new { jobId = job.Id, statusUrl = $"/api/jobs/{job.Id}" }));
});

app.MapPost("/api/jobs/batch", async (BatchRequest req, OcrDbContext db, JobPublisher publisher, ImageStore images, IConfiguration config, CancellationToken ct) =>
{
    var max = config.GetValue("Batch:MaxSize", 10000);
    if (req.Jobs is null || req.Jobs.Count < 1 || req.Jobs.Count > max ||
        req.Jobs.Any(j => string.IsNullOrWhiteSpace(j.ClientReference) || string.IsNullOrWhiteSpace(j.ImageReference) || images.PairDirectory(j.ImageReference) is null))
        return Results.ValidationProblem(new Dictionary<string, string[]> { ["jobs"] = [$"Provide 1 to {max} jobs with clientReference and existing uploaded imageReference"] });
    var batchId = Guid.NewGuid();
    var jobs = req.Jobs.Select(j => NewJob(j.ClientReference, j.ImageReference, batchId, req.BatchName)).ToList();
    return await Submit(jobs, db, publisher, ct,
        () => Results.Accepted("/api/dashboard/snapshot", new { batchId, count = jobs.Count, firstJobId = jobs[0].Id }));
});

// The API only saves files and enqueues references. Model inference never runs here.
async Task<IResult> Upload(HttpRequest request, bool batch, OcrDbContext db, JobPublisher publisher, ImageStore images, CancellationToken ct)
{
    IResult Invalid(string message) => Results.ValidationProblem(new Dictionary<string, string[]> { ["images"] = [message] });
    if (!request.HasFormContentType) return Invalid("Use multipart/form-data with front and back image files");
    IFormCollection form;
    try { form = await request.ReadFormAsync(ct); }
    catch (InvalidDataException) { return Invalid("Invalid multipart upload or upload exceeds 256 MB"); }
    var fronts = form.Files.GetFiles("front");
    var backs = form.Files.GetFiles("back");
    var max = batch ? app.Configuration.GetValue("Batch:MaxUploadSize", 100) : 1;
    if (fronts.Count < 1 || fronts.Count > max || fronts.Count != backs.Count ||
        form.Files.Count != fronts.Count + backs.Count)
        return Invalid($"Upload 1 to {max} matched front/back pairs, using only front and back file fields");
    string[] refs;
    if (batch)
    {
        // Match by person number on the server as well; multipart order is irrelevant.
        if (!BatchImagePairs.TryMatch(form.Files, out var pairs, out var error)) return Invalid(error);
        fronts = pairs.Select(p => p.Front).ToArray();
        backs = pairs.Select(p => p.Back).ToArray();
        refs = pairs.Select(p => p.Reference).ToArray();
    }
    else
    {
        refs = form["clientReference"].Select(r => r ?? "").ToArray();
        if (refs.Length != 1 || string.IsNullOrWhiteSpace(refs[0]))
            return Invalid("Provide a nonempty clientReference");
    }
    for (var i = 0; i < fronts.Count; i++)
        if (!await ImageStore.IsValidAsync(fronts[i], ct) || !await ImageStore.IsValidAsync(backs[i], ct))
            return Invalid("Each image must be JPEG, PNG, WEBP, BMP or TIFF, at most 20 MB");

    var batchId = batch ? Guid.NewGuid() : (Guid?)null;
    var jobs = Enumerable.Range(0, fronts.Count).Select(i =>
        NewJob(refs[i]!, "", batchId, batch ? form["batchName"].ToString() : null)).ToList();
    try
    {
        for (var i = 0; i < jobs.Count; i++)
        {
            jobs[i].ImageReference = jobs[i].Id.ToString("N");
            await images.SaveAsync(jobs[i].Id, fronts[i], backs[i], ct);
        }
    }
    catch
    {
        foreach (var job in jobs) images.Remove(job.Id);
        throw;
    }
    return await Submit(jobs, db, publisher, ct, () => batch
        ? Results.Accepted("/api/dashboard/snapshot", new { batchId, count = jobs.Count, firstJobId = jobs[0].Id })
        : Results.Accepted($"/api/jobs/{jobs[0].Id}", new { jobId = jobs[0].Id, statusUrl = $"/api/jobs/{jobs[0].Id}" }));
}

app.MapPost("/api/jobs/upload", (HttpRequest request, OcrDbContext db, JobPublisher publisher, ImageStore images, CancellationToken ct) =>
    Upload(request, false, db, publisher, images, ct));
app.MapPost("/api/jobs/batch/upload", (HttpRequest request, OcrDbContext db, JobPublisher publisher, ImageStore images, CancellationToken ct) =>
    Upload(request, true, db, publisher, images, ct));

app.MapGet("/api/jobs/{id:guid}/images/{side}", async (Guid id, string side, HttpContext context, OcrDbContext db, ImageStore images, CancellationToken ct) =>
{
    if (side is not ("front" or "back")) return Results.NotFound();
    var job = await db.Jobs.AsNoTracking().FirstOrDefaultAsync(j => j.Id == id, ct);
    var directory = job is null ? null : images.PairDirectory(job.ImageReference);
    if (directory is null) return Results.NotFound();
    var path = Path.Combine(directory, side);
    var header = new byte[16];
    await using (var stream = File.OpenRead(path)) await stream.ReadAtLeastAsync(header, 12, throwOnEndOfStream: false, cancellationToken: ct);
    context.Response.Headers.CacheControl = "no-store";
    context.Response.Headers["X-Content-Type-Options"] = "nosniff";
    return Results.File(path, ImageStore.ContentType(header) ?? "application/octet-stream");
});

app.MapGet("/api/jobs/{id:guid}", async (Guid id, OcrDbContext db, CancellationToken ct) =>
    await db.Jobs.AsNoTracking().FirstOrDefaultAsync(j => j.Id == id, ct) is { } j
        ? Results.Ok(new
        {
            jobId = j.Id, j.CorrelationId, j.BatchId, j.BatchName, j.OcrWorkerId, j.BusinessWorkerId, j.ClientReference, j.ImageReference,
            status = j.Status.ToString(), outcome = j.Outcome?.ToString(), j.ReasonCodes,
            ocr = j.OcrOutcome is null ? null : new { outcome = j.OcrOutcome, j.Confidence, durationMs = j.OcrDurationMs, j.ModelVersion, fields = j.ExtractedFields, details = j.OcrDetails },
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
record BatchRequest(List<SubmitJobRequest>? Jobs, string? BatchName);
