using Microsoft.EntityFrameworkCore;
using OcrDemo.BusinessWorker;
using OcrDemo.Shared;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddOcrDb(builder.Configuration);
builder.Services.AddHostedService<ResultConsumer>();
builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(30));

builder.Build().Run();
