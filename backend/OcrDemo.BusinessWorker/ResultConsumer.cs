using System.Diagnostics;
using System.Text.Json;
using Microsoft.EntityFrameworkCore;
using OcrDemo.Shared;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;

namespace OcrDemo.BusinessWorker;

/// <summary>
/// Consumes ocr-results with prefetch=1 (fair dispatch) and manual acks.
/// Ack happens only after the DB transaction commits. Run as many instances as you like.
/// </summary>
public sealed class ResultConsumer(IServiceScopeFactory scopes, IConfiguration config, ILogger<ResultConsumer> log) : BackgroundService
{
    private readonly string _workerId = $"business-{Environment.MachineName}-{Environment.ProcessId}";
    private readonly double _threshold = config.GetValue("Business:ConfidenceThreshold", 0.75);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await using var connection = await Rabbit.CreateFactory(config).CreateConnectionAsync(_workerId, stoppingToken);
        await using var channel = await connection.CreateChannelAsync(cancellationToken: stoppingToken);
        await Rabbit.DeclareQueueAsync(channel, Queues.OcrResults, stoppingToken);
        await channel.BasicQosAsync(0, 1, false, stoppingToken); // one unacked message at a time

        var consumer = new AsyncEventingBasicConsumer(channel);
        consumer.ReceivedAsync += (_, ea) => HandleAsync(channel, ea, stoppingToken);
        await channel.BasicConsumeAsync(Queues.OcrResults, autoAck: false, consumer, stoppingToken);
        log.LogInformation("{Worker} consuming {Queue}", _workerId, Queues.OcrResults);

        try { await Task.Delay(Timeout.Infinite, stoppingToken); }
        catch (OperationCanceledException) { log.LogInformation("{Worker} shutting down", _workerId); }
    }

    private async Task HandleAsync(IChannel channel, BasicDeliverEventArgs ea, CancellationToken ct)
    {
        OcrResultMessage? msg;
        try { msg = JsonSerializer.Deserialize<OcrResultMessage>(ea.Body.Span, Json.Options); }
        catch (JsonException) { msg = null; }

        if (msg is null)
        {
            log.LogError("Malformed result message, sending to DLQ");
            await channel.BasicNackAsync(ea.DeliveryTag, false, requeue: false, CancellationToken.None);
            return;
        }

        var sw = Stopwatch.StartNew();
        try
        {
            var applied = await ProcessAsync(msg, ct);
            await channel.BasicAckAsync(ea.DeliveryTag, false, CancellationToken.None);
            log.LogInformation("Job {JobId} {Result} in {Ms}ms (corr {Corr})", msg.JobId,
                applied is null ? "duplicate-ignored" : applied.ToString(), sw.ElapsedMilliseconds, msg.CorrelationId);
        }
        catch (KeyNotFoundException)
        {
            log.LogError("Job {JobId} not found, sending to DLQ", msg.JobId); // permanent: retrying will not help
            await channel.BasicNackAsync(ea.DeliveryTag, false, requeue: false, CancellationToken.None);
        }
        catch (Exception ex) when (!ct.IsCancellationRequested)
        {
            // Transient (e.g. DB down): put it back and wait a bit so we don't hot-loop.
            log.LogError(ex, "Job {JobId} failed, requeueing", msg.JobId);
            await Task.Delay(TimeSpan.FromSeconds(3), CancellationToken.None);
            await channel.BasicNackAsync(ea.DeliveryTag, false, requeue: true, CancellationToken.None);
        }
        catch
        {
            // Shutting down mid-message: leave it unacked; the broker redelivers it.
        }
    }

    /// <returns>The final outcome, or null if this was a duplicate delivery (no side effects executed).</returns>
    private async Task<JobOutcome?> ProcessAsync(OcrResultMessage msg, CancellationToken ct)
    {
        using var scope = scopes.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<OcrDbContext>();
        await using var tx = await db.Database.BeginTransactionAsync(ct);

        // Row lock: a concurrent duplicate of the same job waits here, then sees Completed.
        var job = await db.Jobs.FromSqlInterpolated($"""SELECT * FROM jobs WHERE "Id" = {msg.JobId} FOR UPDATE""")
                      .SingleOrDefaultAsync(ct)
                  ?? throw new KeyNotFoundException();

        job.ResultDeliveries++;
        if (job.Status == JobStatus.Completed)
        {
            await db.SaveChangesAsync(ct);
            await tx.CommitAsync(ct);
            return null;
        }

        var (outcome, reasons) = BusinessRules.Evaluate(msg, _threshold);
        job.OcrOutcome = msg.OcrOutcome;
        job.ExtractedFields = msg.ExtractedFields;
        job.OcrDetails = msg.Details;
        job.Confidence = msg.Confidence;
        job.OcrDurationMs = msg.ProcessingMs;
        job.ModelVersion = msg.ModelVersion;
        job.StartedAt = msg.StartedAt.UtcDateTime;
        job.OcrWorkerId = msg.WorkerId;
        job.BusinessWorkerId = _workerId;
        job.Outcome = outcome;
        job.ReasonCodes = reasons;
        job.Status = JobStatus.Completed;
        job.CompletedAt = DateTime.UtcNow;
        await db.SaveChangesAsync(ct);
        await tx.CommitAsync(ct);
        return outcome;
    }
}
