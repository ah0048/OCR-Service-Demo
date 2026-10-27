using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Http;

namespace OcrDemo.Api;

public record BatchImagePair(string Reference, IFormFile Front, IFormFile Back);

public static partial class BatchImagePairs
{
    [GeneratedRegex(@"\A([0-9]+)_([12])\.[^.]+\z", RegexOptions.CultureInvariant)]
    private static partial Regex FilenamePattern();

    public static bool TryMatch(IFormFileCollection files, out List<BatchImagePair> pairs, out string error)
    {
        pairs = [];
        error = "";
        var groups = new Dictionary<string, (IFormFile? Front, IFormFile? Back)>(StringComparer.Ordinal);
        foreach (var file in files)
        {
            var name = Path.GetFileName(file.FileName.Replace('\\', '/'));
            var match = FilenamePattern().Match(name);
            if (!match.Success)
            {
                error = "Batch filenames must be number_1.ext (front) and the same number_2.ext (back), e.g. 3967452_1.jpg and 3967452_2.jpg";
                return false;
            }
            var reference = match.Groups[1].Value;
            var front = match.Groups[2].Value == "1";
            if (file.Name != (front ? "front" : "back"))
            {
                error = $"Image {name} must use the {(front ? "front" : "back")} file field";
                return false;
            }
            var pair = groups.GetValueOrDefault(reference);
            if ((front ? pair.Front : pair.Back) is not null)
            {
                error = $"Duplicate {(front ? "front" : "back")} image for {reference}";
                return false;
            }
            groups[reference] = front ? (file, pair.Back) : (pair.Front, file);
        }
        foreach (var (reference, pair) in groups)
        {
            if (pair.Front is null || pair.Back is null)
            {
                error = $"Missing front or back image for {reference}";
                return false;
            }
            pairs.Add(new(reference, pair.Front, pair.Back));
        }
        if (pairs.Count == 0) { error = "Upload at least one complete numeric front/back pair"; return false; }
        return true;
    }
}
