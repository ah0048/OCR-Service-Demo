using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace OcrDemo.Shared.Migrations
{
    /// <inheritdoc />
    public partial class AddBatchAndWorkerTracking : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<Guid>(
                name: "BatchId",
                table: "jobs",
                type: "uuid",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "BatchName",
                table: "jobs",
                type: "text",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "BusinessWorkerId",
                table: "jobs",
                type: "text",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "OcrWorkerId",
                table: "jobs",
                type: "text",
                nullable: true);

            migrationBuilder.CreateIndex(
                name: "IX_jobs_BatchId",
                table: "jobs",
                column: "BatchId");

            migrationBuilder.CreateIndex(
                name: "IX_jobs_CreatedAt",
                table: "jobs",
                column: "CreatedAt");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropIndex(
                name: "IX_jobs_BatchId",
                table: "jobs");

            migrationBuilder.DropIndex(
                name: "IX_jobs_CreatedAt",
                table: "jobs");

            migrationBuilder.DropColumn(
                name: "BatchId",
                table: "jobs");

            migrationBuilder.DropColumn(
                name: "BatchName",
                table: "jobs");

            migrationBuilder.DropColumn(
                name: "BusinessWorkerId",
                table: "jobs");

            migrationBuilder.DropColumn(
                name: "OcrWorkerId",
                table: "jobs");
        }
    }
}
