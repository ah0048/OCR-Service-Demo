using System.Net;
using Microsoft.Extensions.Logging.Abstractions;
using OcrDemo.Api;

static class RabbitManagementChecks
{
    public static async Task<int> RunAsync()
    {
        var checks = 0;
        void Check(bool ok, string message) { if (!ok) throw new Exception(message); checks++; }
        async Task<RabbitSnapshot?> Snapshot(string consumers, string queues = """[{"name":"ocr-jobs","message_stats":null}]""",
            string channels = """[{"name":"channel-1","messages_unacknowledged":1}]""",
            string connections = """[{"name":"connection-1","client_properties":{"connection_name":"ocr-test-123"}}]""")
        {
            using var http = new HttpClient(new ManagementHandler(new()
            {
                ["/api/queues"] = queues, ["/api/channels"] = channels,
                ["/api/connections"] = connections, ["/api/consumers"] = consumers
            })) { BaseAddress = new Uri("http://rabbit.test") };
            return await new RabbitManagement(http, NullLogger<RabbitManagement>.Instance).GetAsync(CancellationToken.None);
        }

        var normal = await Snapshot("""[{"queue":{"name":"ocr-jobs"},"channel_details":{"name":"channel-1","connection_name":"connection-1"},"prefetch_count":1}]""");
        Check(normal?.Consumers.Single() == new ConsumerStat("ocr-jobs", "ocr-test-123", 1, 1), "Normal consumer identity and busy stats preserved");
        var missingName = await Snapshot("""[{"queue":{"name":"ocr-jobs"},"channel_details":{"connection_name":"connection-1"},"prefetch_count":1}]""");
        Check(missingName?.Consumers.Single().Connection == "ocr-test-123", "Missing channel name must not lose named worker or throw");
        Check(missingName?.Consumers.Single().ChannelUnacked == 0, "Unknown channel busy stats default to zero");
        var partial = await Snapshot("""[null,{},{"queue":null},{"queue":{"name":"ocr-jobs"},"channel_details":null},{"queue":{"name":"ocr-jobs"},"channel_details":{}},{"queue":{"name":"ocr-results"},"channel_details":{"name":"channel-1"}}]""");
        Check(partial?.Consumers.Count == 1 && partial.Consumers[0].Queue == "ocr-results", "Malformed consumers skipped without losing valid consumers");
        Check(partial?.Consumers[0].Connection == "channel-1", "Channel identity retained if connection identity missing");
        var optional = await Snapshot("""[{"queue":{"name":"ocr-jobs"},"channel_details":{"connection_name":"connection-1"},"prefetch_count":999999999999999}]""",
            queues: """[null,{},{"name":7},{"name":"ocr-jobs","messages":999999999999999,"message_stats":{"publish_details":null}}]""",
            channels: """[null,{},{"name":null},{"name":"channel-1","messages_unacknowledged":"unknown"}]""",
            connections: """[null,{},{"name":"connection-1","client_properties":null}]""");
        Check(optional?.Queues.Count == 1 && optional.Queues[0].Total == 0 && optional.Queues[0].PublishRate == 0, "Missing/invalid optional queue stats safe");
        Check(optional?.Consumers.Single().Connection == "connection-1" && optional.Consumers[0].Prefetch == 0, "Missing connection registration and invalid prefetch safe");
        Check(await Snapshot("{}") is null, "Unexpected top-level response treated as unavailable");
        Check(await Snapshot("not-json") is null, "Malformed JSON treated as unavailable");
        Console.WriteLine($"PASS: {checks} RabbitMQ management parsing checks.");
        return checks;
    }

    private sealed class ManagementHandler(Dictionary<string, string> responses) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(responses[request.RequestUri!.AbsolutePath])
            });
    }
}
