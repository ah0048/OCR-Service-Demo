using Microsoft.EntityFrameworkCore.Migrations;
using System.Text.Json;

#nullable disable

namespace OcrDemo.Shared.Migrations;

public partial class AddOcrDetails : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder) =>
        migrationBuilder.AddColumn<JsonDocument>(name: "OcrDetails", table: "jobs", type: "jsonb", nullable: true);

    protected override void Down(MigrationBuilder migrationBuilder) =>
        migrationBuilder.DropColumn(name: "OcrDetails", table: "jobs");
}
