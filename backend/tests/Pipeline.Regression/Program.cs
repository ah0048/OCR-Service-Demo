using System.Text.Json;
using Microsoft.AspNetCore.Http;
using OcrDemo.Api;
using OcrDemo.BusinessWorker;
using OcrDemo.Shared;

var checks = 0;
void Check(bool value, string label) { if (!value) throw new Exception(label); checks++; }
OcrResultMessage Result(string outcome, double? confidence = .95, string? details = null) =>
    new(Guid.NewGuid(), Guid.NewGuid(), outcome,
        new() { ["nationalId"] = "29001010123456", ["fullName"] = "Test Name", ["birthDate"] = "1990-01-01", ["expiryDate"] = "2035-01-01" },
        confidence, 100, DateTimeOffset.UtcNow, "test", "test-worker",
        details is null ? null : JsonDocument.Parse(details));
Check(BusinessRules.Evaluate(Result("Rejected"), .75).Outcome == JobOutcome.Rejected, "Rejected must beat confidence");
Check(BusinessRules.Evaluate(Result("LowConfidence", .99, """{"national_id":{"status":"conflict"}}"""), .75).Outcome == JobOutcome.Rejected, "Legacy conflict must be rejected");
Check(BusinessRules.Evaluate(Result("Unreadable", 0, """{"status":"id_mismatch"}"""), .75).Outcome == JobOutcome.Rejected, "Mismatch must beat unreadable");
Check(BusinessRules.Evaluate(Result("LowConfidence", .99), .75).Outcome == JobOutcome.Succeeded, "Passing confidence overrides model review flags");
Check(BusinessRules.Evaluate(Result("Success"), .75).Outcome == JobOutcome.Succeeded, "Valid result");
Check(BusinessRules.Evaluate(Result("Success", .5), .75).Outcome == JobOutcome.ManualReview, "Low aggregate requires review");
Check(BusinessRules.Evaluate(Result("TechnicalError"), .75).Outcome == JobOutcome.TechnicalError, "Technical errors stay distinct");
Check(BusinessRules.Evaluate(Result("Rejected"), .75).ReasonCodes.SequenceEqual(["FRONT_BACK_ID_MISMATCH"]), "Mismatch reason preserved");
Check(BusinessRules.Evaluate(Result("Success", .75), .75).Outcome == JobOutcome.Succeeded, "Exact 75 percent passes the confidence gate");
Check(BusinessRules.Evaluate(Result("Success", .7499), .75).Outcome == JobOutcome.ManualReview, "Below 75 percent requires review");
Check(BusinessRules.Evaluate(Result("Success", null), .75).Outcome == JobOutcome.ManualReview, "Missing score requires review");
Check(BusinessRules.Evaluate(Result("LowConfidence", .75, """{"review_required":true}"""), .75).Outcome == JobOutcome.Succeeded, "Exact threshold overrides model review flag");
Check(BusinessRules.Evaluate(Result("LowConfidence", .7499), .75).Outcome == JobOutcome.ManualReview, "Review-flagged low score stays manual");
var expiredAndIncomplete = Result("LowConfidence", .9) with { ExtractedFields = new() { ["expiryDate"] = "2020-01-01", ["nationalId"] = "invalid" } };
Check(BusinessRules.Evaluate(expiredAndIncomplete, .75).Outcome == JobOutcome.Succeeded, "Passing score is sufficient despite missing/invalid fields and expiry");
Check(BusinessRules.Evaluate(Result("Unreadable", .99), .75).Outcome == JobOutcome.OcrFailed, "Unreadable cannot be accepted by a high score");
Check(BusinessRules.Evaluate(Result("TechnicalError", .99), .75).Outcome == JobOutcome.TechnicalError, "Technical failure cannot be accepted by a high score");
Check(BusinessRules.Evaluate(Result("Success", double.NaN), .75).Outcome == JobOutcome.ManualReview, "Nonfinite score requires review");
Check(BusinessRules.Evaluate(Result("Success", 1.1), .75).Outcome == JobOutcome.ManualReview, "Out-of-range score requires review");
Check(BusinessRules.Evaluate(Result("Success", .85), .9).Outcome == JobOutcome.ManualReview, "Configured threshold is respected");
await RabbitManagementChecks.RunAsync();
IFormFile File(string field, string name) => new FormFile(new MemoryStream([1]), 0, 1, field, name);
bool Match(params IFormFile[] files) => BatchImagePairs.TryMatch(new FormFileCollection { Capacity = files.Length }.With(files), out _, out _);
var shuffled = new FormFileCollection {
    File("back", "3967453_2.png"), File("front", "3967452_1.jpg"),
    File("front", "3967453_1.jpg"), File("back", "3967452_2.png")
};
Check(BatchImagePairs.TryMatch(shuffled, out var pairs, out _), "Shuffled pairs accepted");
Check(pairs.Count == 2 && pairs.All(p => p.Front.FileName.StartsWith(p.Reference + "_1.") && p.Back.FileName.StartsWith(p.Reference + "_2.")), "Pair by number independent of order and extension");
Check(!Match(File("front", "3967452_1.jpg")), "Missing back rejected");
Check(!Match(File("front", "3967452_1.jpg"), File("back", "3967453_2.jpg")), "Different person numbers rejected");
Check(!Match(File("front", "3967452_1.jpg"), File("front", "3967452_1.png"), File("back", "3967452_2.jpg")), "Duplicate side rejected");
Check(!Match(File("front", "CLIENT_front.jpg"), File("back", "CLIENT_back.jpg")), "Old naming rejected");
Check(!Match(File("front", "abc_1.jpg"), File("back", "abc_2.jpg")), "Nonnumeric references rejected");
Check(!Match(File("front", "3967452_2.jpg"), File("back", "3967452_1.jpg")), "Reversed side fields rejected");
Check(!Match(File("front", "3967452_3.jpg"), File("back", "3967452_2.jpg")), "Only suffix 1/2 accepted");
Console.WriteLine($"PASS: {checks} rejection, business rule, and numeric batch checks.");

static class FilesExtensions
{
    public static FormFileCollection With(this FormFileCollection collection, IEnumerable<IFormFile> files)
    {
        foreach (var file in files) collection.Add(file);
        return collection;
    }
}
