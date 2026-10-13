using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Npgsql;
using Microsoft.EntityFrameworkCore;

namespace OcrDemo.Shared;

public enum JobStatus { Queued, Completed }

public enum JobOutcome { Succeeded, ValidationFailed, ManualReview, OcrFailed, TechnicalError }

public class Job
{
    public Guid Id { get; set; }
    public Guid CorrelationId { get; set; }
    public string ClientReference { get; set; } = "";
    public string ImageReference { get; set; } = "";
    public JobStatus Status { get; set; }
    public JobOutcome? Outcome { get; set; }
    public List<string> ReasonCodes { get; set; } = [];
    public Dictionary<string, string>? ExtractedFields { get; set; }
    public double? Confidence { get; set; }
    public string? OcrOutcome { get; set; }
    public int? OcrDurationMs { get; set; }
    public string? ModelVersion { get; set; }
    public DateTime CreatedAt { get; set; }
    public DateTime? StartedAt { get; set; }
    public DateTime? CompletedAt { get; set; }
    public int ResultDeliveries { get; set; }
}

public class OcrDbContext(DbContextOptions<OcrDbContext> options) : DbContext(options)
{
    public DbSet<Job> Jobs => Set<Job>();

    protected override void OnModelCreating(ModelBuilder b)
    {
        b.Entity<Job>(e =>
        {
            e.ToTable("jobs");
            e.HasKey(j => j.Id);
            e.Property(j => j.Status).HasConversion<string>().HasMaxLength(32);
            e.Property(j => j.Outcome).HasConversion<string>().HasMaxLength(32);
            e.Property(j => j.ReasonCodes).HasColumnType("jsonb");
            e.Property(j => j.ExtractedFields).HasColumnType("jsonb");
            e.HasIndex(j => j.Status);
        });
    }
}

public static class DbRegistration
{
    public static IServiceCollection AddOcrDb(this IServiceCollection services, IConfiguration config)
    {
        // Dynamic JSON lets Npgsql write Dictionary/List properties into jsonb columns.
        var dataSource = new NpgsqlDataSourceBuilder(config.GetConnectionString("Default")).EnableDynamicJson().Build();
        return services.AddDbContext<OcrDbContext>(o => o.UseNpgsql(dataSource, n => n.MigrationsAssembly("OcrDemo.Shared")));
    }
}
