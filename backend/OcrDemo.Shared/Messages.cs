using System.Text.Json;

namespace OcrDemo.Shared;

public static class Queues
{
    public const string OcrJobs = "ocr-jobs";
    public const string OcrResults = "ocr-results";
    public static string Dlq(string queue) => queue + ".dlq";
}

public static class Json
{
    public static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web);
}

/// <summary>Sent API -> Python OCR worker. No image bytes, only a reference.</summary>
public record OcrJobMessage(Guid JobId, Guid CorrelationId, string ClientReference, string ImageReference);

/// <summary>Sent Python OCR worker -> business worker. Outcome: Success | Unreadable | LowConfidence.</summary>
public record OcrResultMessage(
    Guid JobId,
    Guid CorrelationId,
    string OcrOutcome,
    Dictionary<string, string>? ExtractedFields,
    double Confidence,
    int ProcessingMs,
    DateTimeOffset StartedAt,
    string ModelVersion);
