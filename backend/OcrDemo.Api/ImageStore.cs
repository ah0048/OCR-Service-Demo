namespace OcrDemo.Api;

/// <summary>Shared local image store. Mount the same directory on every OCR host.</summary>
public sealed class ImageStore(IConfiguration config, IWebHostEnvironment environment)
{
    public const long MaxImageBytes = 20 * 1024 * 1024;
    public const long MaxUploadBytes = 256 * 1024 * 1024;
    private readonly string _root = Path.GetFullPath(config["Images:Root"]
        ?? Path.Combine(environment.ContentRootPath, "../../.data/images"));

    public string? PairDirectory(string reference)
    {
        // API-created references are opaque IDs, never paths supplied by callers.
        if (!Guid.TryParseExact(reference, "N", out var id)) return null;
        var path = Path.Combine(_root, id.ToString("N"));
        return File.Exists(Path.Combine(path, "front")) && File.Exists(Path.Combine(path, "back")) ? path : null;
    }

    public static string? ContentType(ReadOnlySpan<byte> header)
    {
        if (header.StartsWith(new byte[] { 0xff, 0xd8, 0xff })) return "image/jpeg";
        if (header.StartsWith(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 })) return "image/png";
        if (header.Length >= 12 && header[..4].SequenceEqual("RIFF"u8) && header[8..12].SequenceEqual("WEBP"u8)) return "image/webp";
        if (header.StartsWith("BM"u8)) return "image/bmp";
        if (header.StartsWith(new byte[] { 73, 73, 42, 0 }) || header.StartsWith(new byte[] { 77, 77, 0, 42 })) return "image/tiff";
        return null;
    }

    public static async Task<bool> IsValidAsync(IFormFile image, CancellationToken ct)
    {
        if (image.Length is < 1 or > MaxImageBytes) return false;
        await using var stream = image.OpenReadStream();
        var header = new byte[16];
        var length = await stream.ReadAtLeastAsync(header, 12, throwOnEndOfStream: false, cancellationToken: ct);
        return ContentType(header.AsSpan(0, length)) is not null;
    }

    public async Task SaveAsync(Guid jobId, IFormFile front, IFormFile back, CancellationToken ct)
    {
        var directory = Path.Combine(_root, jobId.ToString("N"));
        Directory.CreateDirectory(directory);
        foreach (var (side, file) in new[] { ("front", front), ("back", back) })
        {
            await using var output = new FileStream(Path.Combine(directory, side), FileMode.CreateNew);
            await file.CopyToAsync(output, ct);
        }
    }

    public void Remove(Guid jobId)
    {
        // Only generated job directories inside the configured root can be removed.
        var directory = Path.Combine(_root, jobId.ToString("N"));
        if (Directory.Exists(directory)) Directory.Delete(directory, recursive: true);
    }
}
