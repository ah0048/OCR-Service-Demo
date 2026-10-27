using OcrDemo.Shared;

namespace OcrDemo.BusinessWorker;

/// <summary>Business rules. Pure function of the OCR result; independent of how OCR was produced.</summary>
public static class BusinessRules
{
    private static bool HasIdMismatch(OcrResultMessage r)
    {
        if (r.Details is null || r.Details.RootElement.ValueKind != System.Text.Json.JsonValueKind.Object) return false;
        var details = r.Details.RootElement;
        return (details.TryGetProperty("status", out var status) && status.ValueKind == System.Text.Json.JsonValueKind.String && status.GetString() == "id_mismatch") ||
               (details.TryGetProperty("national_id", out var id) && id.ValueKind == System.Text.Json.JsonValueKind.Object &&
                id.TryGetProperty("status", out var agreement) && agreement.ValueKind == System.Text.Json.JsonValueKind.String && agreement.GetString() == "conflict");
    }

    public static (JobOutcome Outcome, List<string> ReasonCodes) Evaluate(OcrResultMessage r, double confidenceThreshold)
    {
        if (r.OcrOutcome == "Rejected" || HasIdMismatch(r))
            return (JobOutcome.Rejected, ["FRONT_BACK_ID_MISMATCH"]);

        if (r.OcrOutcome == "TechnicalError")
            return (JobOutcome.TechnicalError, ["OCR_TECHNICAL_ERROR"]);

        if (r.OcrOutcome == "Unreadable" || r.ExtractedFields is null)
            return (JobOutcome.OcrFailed, ["OCR_UNREADABLE"]);

        if (r.Confidence is not { } confidence || !double.IsFinite(confidence) || confidence < 0 || confidence > 1)
            return (JobOutcome.ManualReview, ["OCR_CONFIDENCE_UNAVAILABLE"]);

        if (confidence < confidenceThreshold)
            return (JobOutcome.ManualReview, ["LOW_CONFIDENCE"]);

        // Passing the configured score is sufficient for acceptance. Model
        // review flags and field-format/expiry checks no longer block it.
        return (JobOutcome.Succeeded, []);
    }
}
