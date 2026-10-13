using System.Text.Json;
using OcrDemo.Shared;
using RabbitMQ.Client;

namespace OcrDemo.Api;

/// <summary>Publishes OCR jobs directly to RabbitMQ (persistent messages, publisher confirms). One shared channel, serialized with a lock.</summary>
public sealed class JobPublisher(IConfiguration config) : IAsyncDisposable
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private IConnection? _connection;
    private IChannel? _channel;

    private async Task<IChannel> GetChannelAsync(CancellationToken ct)
    {
        if (_channel is { IsOpen: true }) return _channel;
        _connection ??= await Rabbit.CreateFactory(config).CreateConnectionAsync("ocr-api", ct);
        _channel = await _connection.CreateChannelAsync(
            new CreateChannelOptions(publisherConfirmationsEnabled: true, publisherConfirmationTrackingEnabled: true), ct);
        await Rabbit.DeclareQueueAsync(_channel, Queues.OcrJobs, ct);
        return _channel;
    }

    public async Task PublishAsync(IEnumerable<OcrJobMessage> messages, CancellationToken ct)
    {
        await _lock.WaitAsync(ct);
        try
        {
            var channel = await GetChannelAsync(ct);
            foreach (var m in messages)
            {
                var props = new BasicProperties
                {
                    Persistent = true,
                    ContentType = "application/json",
                    MessageId = m.JobId.ToString(),
                    CorrelationId = m.CorrelationId.ToString(),
                };
                await channel.BasicPublishAsync("", Queues.OcrJobs, mandatory: false, props,
                    JsonSerializer.SerializeToUtf8Bytes(m, Json.Options), ct);
            }
        }
        finally { _lock.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        if (_channel is not null) await _channel.DisposeAsync();
        if (_connection is not null) await _connection.DisposeAsync();
    }
}
