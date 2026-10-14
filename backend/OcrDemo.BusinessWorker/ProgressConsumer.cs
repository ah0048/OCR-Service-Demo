using System.Text.Json;
using Microsoft.EntityFrameworkCore;
using OcrDemo.Shared;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;

namespace OcrDemo.BusinessWorker;

/// <summary>
/// Consumes "OCR started" events and moves a job Queued -> OcrInProgress so the dashboard can show it.
/// Purely informational: the guarded UPDATE never touches a job that has moved on, so late or duplicate
/// events are harmless, and a lost event only means the job skips the in-progress state.
/// </summary>
public sealed class ProgressConsumer(IServiceScopeFactory scopes, IConfiguration config, ILogger<ProgressConsumer> log) : BackgroundService
{
    private readonly string _name = $"business-events-{Environment.MachineName}-{Environment.ProcessId}";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await using var connection = await Rabbit.CreateFactory(config).CreateConnectionAsync(_name, stoppingToken);
        await using var channel = await connection.CreateChannelAsync(cancellationToken: stoppingToken);
        await Rabbit.DeclareQueueAsync(channel, Queues.JobEvents, stoppingToken);
        await channel.BasicQosAsync(0, 1, false, stoppingToken);

        var consumer = new AsyncEventingBasicConsumer(channel);
        consumer.ReceivedAsync += async (_, ea) =>
        {
            try
            {
                var msg = JsonSerializer.Deserialize<JobStartedMessage>(ea.Body.Span, Json.Options);
                if (msg is not null)
                {
                    using var scope = scopes.CreateScope();
                    var db = scope.ServiceProvider.GetRequiredService<OcrDbContext>();
                    await db.Jobs.Where(j => j.Id == msg.JobId && j.Status == JobStatus.Queued)
                        .ExecuteUpdateAsync(s => s
                            .SetProperty(j => j.Status, JobStatus.OcrInProgress)
                            .SetProperty(j => j.StartedAt, msg.StartedAt.UtcDateTime)
                            .SetProperty(j => j.OcrWorkerId, msg.WorkerId), stoppingToken);
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                log.LogWarning(ex, "Ignoring bad or failed job-started event");
            }
            // Always ack: events are best-effort, never worth retrying or dead-lettering.
            await channel.BasicAckAsync(ea.DeliveryTag, false, CancellationToken.None);
        };
        await channel.BasicConsumeAsync(Queues.JobEvents, autoAck: false, consumer, stoppingToken);

        try { await Task.Delay(Timeout.Infinite, stoppingToken); }
        catch (OperationCanceledException) { }
    }
}
