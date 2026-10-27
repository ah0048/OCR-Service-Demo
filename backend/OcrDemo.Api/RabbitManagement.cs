using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace OcrDemo.Api;

public record QueueStat(string Name, string Role, int Ready, int Unacked, int Total, int Consumers,
    double PublishRate, double DeliverRate, double AckRate, long Published, long Acked, long Redelivered, long Delivered);

public record ConsumerStat(string Queue, string Connection, int Prefetch, int ChannelUnacked);

public record RabbitSnapshot(List<QueueStat> Queues, List<ConsumerStat> Consumers);

/// <summary>
/// Reads queue and consumer statistics from the RabbitMQ management HTTP API, so the dashboard
/// does not need the RabbitMQ UI. Results are cached for a moment so many dashboard viewers
/// don't multiply the load on the broker.
/// </summary>
public sealed class RabbitManagement(HttpClient http, ILogger<RabbitManagement> log)
{
    private static readonly string[] Known = ["ocr-jobs", "ocr-jobs.dlq", "ocr-results", "ocr-results.dlq", "job-events", "job-events.dlq"];
    private readonly SemaphoreSlim _lock = new(1, 1);
    private readonly Dictionary<string, List<(DateTime T, long Pub, long Del, long Ack)>> _samples = new();
    private RabbitSnapshot? _cached;
    private DateTime _cachedAt;

    public static void Configure(HttpClient client, IConfiguration config)
    {
        client.BaseAddress = new Uri(config["RabbitMq:ManagementUrl"] ?? "http://localhost:15672");
        client.Timeout = TimeSpan.FromSeconds(3);
        var cred = $"{config["RabbitMq:User"] ?? "ocr"}:{config["RabbitMq:Password"] ?? "ocr_dev_pw"}";
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Basic", Convert.ToBase64String(Encoding.UTF8.GetBytes(cred)));
    }

    /// <returns>null when the management API cannot be reached.</returns>
    public async Task<RabbitSnapshot?> GetAsync(CancellationToken ct)
    {
        await _lock.WaitAsync(ct);
        try
        {
            if (_cached is not null && DateTime.UtcNow - _cachedAt < TimeSpan.FromMilliseconds(700)) return _cached;
            try
            {
                var queues = WithRates(ParseQueues(await http.GetStringAsync("/api/queues", ct)));
                var channels = ParseChannels(await http.GetStringAsync("/api/channels", ct));
                var names = ParseConnectionNames(await http.GetStringAsync("/api/connections", ct));
                var consumers = ParseConsumers(await http.GetStringAsync("/api/consumers", ct), channels, names);
                _cached = new RabbitSnapshot(queues, consumers);
                _cachedAt = DateTime.UtcNow;
                return _cached;
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
            {
                if (ct.IsCancellationRequested) throw;
                log.LogWarning("RabbitMQ management API unavailable: {Message}", ex.Message);
                return null;
            }
        }
        finally { _lock.Release(); }
    }

    /// <summary>
    /// RabbitMQ's own rate fields lag and often read 0 between samples, so rates are computed here
    /// from counter deltas over a ~10 second sliding window (the management plugin publishes counters in bursts every few seconds).
    /// </summary>
    private List<QueueStat> WithRates(List<QueueStat> queues)
    {
        var now = DateTime.UtcNow;
        return queues.Select(q =>
        {
            if (!_samples.TryGetValue(q.Name, out var list)) _samples[q.Name] = list = [];
            list.Add((now, q.Published, q.Delivered, q.Acked));
            list.RemoveAll(s => (now - s.T).TotalSeconds > 10);
            var first = list[0];
            var dt = (now - first.T).TotalSeconds;
            if (dt < 1) return q;
            double Rate(long a, long b) => Math.Round(Math.Max(0, (b - a) / dt), 2);
            return q with { PublishRate = Rate(first.Pub, q.Published), DeliverRate = Rate(first.Del, q.Delivered), AckRate = Rate(first.Ack, q.Acked) };
        }).ToList();
    }

    private static List<QueueStat> ParseQueues(string json)
    {
        var found = new Dictionary<string, QueueStat>();
        using var doc = ReadArray(json);
        foreach (var q in doc.RootElement.EnumerateArray())
        {
            var name = String(q, "name");
            if (name is null || !Known.Contains(name)) continue;
            q.TryGetProperty("message_stats", out var ms);
            found[name] = new QueueStat(
                name,
                name.EndsWith(".dlq") ? "dead-letter" : name == "job-events" ? "events" : name == "ocr-jobs" ? "jobs" : "results",
                Int(q, "messages_ready"), Int(q, "messages_unacknowledged"), Int(q, "messages"), Int(q, "consumers"),
                Rate(ms, "publish_details"), Rate(ms, "deliver_get_details"), Rate(ms, "ack_details"),
                Long(ms, "publish"), Long(ms, "ack"), Long(ms, "redeliver"), Long(ms, "deliver_get"));
        }
        // Keep a stable order, and show queues that exist only once something declared them.
        return Known.Where(found.ContainsKey).Select(n => found[n]).ToList();
    }

    private static Dictionary<string, int> ParseChannels(string json)
    {
        var result = new Dictionary<string, int>();
        using var doc = ReadArray(json);
        foreach (var c in doc.RootElement.EnumerateArray())
        {
            var name = String(c, "name");
            if (!string.IsNullOrWhiteSpace(name)) result[name] = Int(c, "messages_unacknowledged");
        }
        return result;
    }

    /// <summary>The broker identifies connections by address; map that to the name each worker registered (e.g. "ocr-HOST-1234").</summary>
    private static Dictionary<string, string> ParseConnectionNames(string json)
    {
        var result = new Dictionary<string, string>();
        using var doc = ReadArray(json);
        foreach (var c in doc.RootElement.EnumerateArray())
        {
            var address = String(c, "name");
            if (string.IsNullOrWhiteSpace(address)) continue;
            var registered = c.TryGetProperty("client_properties", out var cp) ? String(cp, "connection_name") : null;
            result[address] = string.IsNullOrWhiteSpace(registered) ? address : registered;
        }
        return result;
    }

    private static List<ConsumerStat> ParseConsumers(string json, Dictionary<string, int> channelUnacked, Dictionary<string, string> names)
    {
        var result = new List<ConsumerStat>();
        using var doc = ReadArray(json);
        foreach (var c in doc.RootElement.EnumerateArray())
        {
            // Management metadata can be incomplete while workers connect/disconnect.
            // A connection identity is sufficient to count a consumer even without a channel name.
            if (c.ValueKind != JsonValueKind.Object ||
                !c.TryGetProperty("queue", out var queue) ||
                !c.TryGetProperty("channel_details", out var details)) continue;
            var queueName = String(queue, "name");
            var channel = String(details, "name");
            var connection = String(details, "connection_name");
            if (string.IsNullOrWhiteSpace(connection)) connection = channel;
            if (string.IsNullOrWhiteSpace(queueName) || string.IsNullOrWhiteSpace(connection)) continue;
            result.Add(new ConsumerStat(
                queueName, names.GetValueOrDefault(connection, connection),
                Int(c, "prefetch_count"), channel is null ? 0 : channelUnacked.GetValueOrDefault(channel)));
        }
        return result;
    }

    private static JsonDocument ReadArray(string json)
    {
        var doc = JsonDocument.Parse(json);
        if (doc.RootElement.ValueKind == JsonValueKind.Array) return doc;
        doc.Dispose();
        throw new JsonException("Expected a RabbitMQ management array response.");
    }

    private static string? String(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() : null;

    private static int Int(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var n) ? n : 0;

    private static long Long(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt64(out var n) ? n : 0;

    private static double Rate(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var d) && d.ValueKind == JsonValueKind.Object &&
        d.TryGetProperty("rate", out var r) && r.ValueKind == JsonValueKind.Number &&
        r.TryGetDouble(out var n) && double.IsFinite(n) ? Math.Round(n, 2) : 0;
}
