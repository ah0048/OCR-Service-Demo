using System.Globalization;
using OcrDemo.Shared;

namespace OcrDemo.BusinessWorker;

/// <summary>Mock business rules. Pure function of the OCR result; independent of how OCR was produced.</summary>
public static class BusinessRules
{
    public static (JobOutcome Outcome, List<string> ReasonCodes) Evaluate(OcrResultMessage r, double confidenceThreshold, DateTime today)
    {
        if (r.OcrOutcome == "Unreadable" || r.ExtractedFields is null)
            return (JobOutcome.OcrFailed, ["OCR_UNREADABLE"]);

        if (r.OcrOutcome == "LowConfidence" || r.Confidence < confidenceThreshold)
            return (JobOutcome.ManualReview, ["LOW_CONFIDENCE"]);

        var codes = new List<string>();
        var f = r.ExtractedFields;

        if (!f.TryGetValue("fullName", out var name) || string.IsNullOrWhiteSpace(name))
            codes.Add("NAME_MISSING");

        DateTime birth = default;
        var birthOk = f.TryGetValue("birthDate", out var b) &&
                      DateTime.TryParseExact(b, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out birth);
        if (!birthOk || birth > today) codes.Add("BIRTHDATE_INVALID");

        if (!f.TryGetValue("nationalId", out var id) || id.Length != 14 || !id.All(char.IsAsciiDigit))
            codes.Add("NATIONAL_ID_FORMAT");
        else if (birthOk)
        {
            // Egyptian ID layout: C YYMMDD GG NNNN K  (C: 2 = 1900s, 3 = 2000s)
            var century = id[0] == '2' ? 1900 : id[0] == '3' ? 2000 : 0;
            if (century == 0 || century + int.Parse(id[1..3]) != birth.Year ||
                int.Parse(id[3..5]) != birth.Month || int.Parse(id[5..7]) != birth.Day)
                codes.Add("NATIONAL_ID_BIRTHDATE_MISMATCH");
        }

        if (!f.TryGetValue("expiryDate", out var e) ||
            !DateTime.TryParseExact(e, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var expiry))
            codes.Add("EXPIRY_INVALID");
        else if (expiry < today)
            codes.Add("ID_EXPIRED");

        return codes.Count == 0 ? (JobOutcome.Succeeded, []) : (JobOutcome.ValidationFailed, codes);
    }
}
