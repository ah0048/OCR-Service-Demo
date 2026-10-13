using Microsoft.Extensions.Configuration;
using RabbitMQ.Client;

namespace OcrDemo.Shared;

public static class Rabbit
{
    public static ConnectionFactory CreateFactory(IConfiguration config) => new()
    {
        HostName = config["RabbitMq:Host"] ?? "localhost",
        Port = int.Parse(config["RabbitMq:Port"] ?? "5672"),
        UserName = config["RabbitMq:User"] ?? "ocr",
        Password = config["RabbitMq:Password"] ?? "ocr_dev_pw",
        AutomaticRecoveryEnabled = true,
    };

    /// <summary>
    /// Declares a durable classic queue. Messages rejected without requeue go to "{queue}.dlq"
    /// (via the default exchange). Arguments must match what the Python worker declares.
    /// </summary>
    public static async Task DeclareQueueAsync(IChannel channel, string queue, CancellationToken ct = default)
    {
        await channel.QueueDeclareAsync(Queues.Dlq(queue), durable: true, exclusive: false, autoDelete: false, cancellationToken: ct);
        await channel.QueueDeclareAsync(queue, durable: true, exclusive: false, autoDelete: false,
            arguments: new Dictionary<string, object?>
            {
                ["x-dead-letter-exchange"] = "",
                ["x-dead-letter-routing-key"] = Queues.Dlq(queue),
            }, cancellationToken: ct);
    }
}
