using System.Text.Json;

namespace OcrDemo.Shared;

public static class Queues
{
    public const string OcrJobs = "ocr-jobs";
    public const string OcrResults = "ocr-results";
    /// <summary>Informational "OCR started" events so the dashboard can show an in-progress step. Losing one is harmless.</summary>
    public const string JobEvents = "job-events";
    public static string Dlq(string queue) => queue + ".dlq";
}

public static class Json
{
    public static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web);
}

/// <summary>Sent API -> Python OCR worker. No image bytes, only a reference.</summary>
public record OcrJobMessage(Guid JobId, Guid CorrelationId, string ClientReference, string ImageReference);

/// <summary>Sent Python OCR worker -> business worker when it picks a job up (best effort).</summary>
public record JobStartedMessage(Guid JobId, string WorkerId, DateTimeOffset StartedAt);

/// <summary>Sent Python OCR worker -> business worker. Outcome: Success | Unreadable | LowConfidence | Rejected | TechnicalError.</summary>
public record OcrResultMessage(
    Guid JobId,
    Guid CorrelationId,
    string OcrOutcome,
    Dictionary<string, string>? ExtractedFields,
    double? Confidence,
    int ProcessingMs,
    DateTimeOffset StartedAt,
    string ModelVersion,
    string? WorkerId,
    JsonDocument? Details = null);
