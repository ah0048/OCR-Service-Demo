"""OCR worker: consumes `ocr-jobs`, runs inference, publishes to `ocr-results`.

Fair dispatch: prefetch_count=1, manual acks. The job is acked only after the result is
published (with publisher confirms). Run several processes to scale out.
"""
import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone

import pika

from inference import MODEL_VERSION, run_ocr

RABBIT_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBIT_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBIT_USER = os.getenv("RABBITMQ_USER", "ocr")
RABBIT_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "ocr_dev_pw")
JOBS_QUEUE = "ocr-jobs"
RESULTS_QUEUE = "ocr-results"
WORKER_ID = f"ocr-{socket.gethostname()}-{os.getpid()}"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"ts": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "worker": WORKER_ID, "msg": record.getMessage()}
        data.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler])
log = logging.getLogger("ocr-worker")


def info(msg, **fields):
    log.info(msg, extra={"extra_fields": fields})


def declare_queue(channel, queue):
    """Same arguments as the .NET side declares: durable + dead-letter to '<queue>.dlq'."""
    channel.queue_declare(queue=queue + ".dlq", durable=True)
    channel.queue_declare(queue=queue, durable=True, arguments={
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": queue + ".dlq",
    })


def connect():
    params = pika.ConnectionParameters(
        host=RABBIT_HOST, port=RABBIT_PORT,
        credentials=pika.PlainCredentials(RABBIT_USER, RABBIT_PASSWORD),
        heartbeat=600, client_properties={"connection_name": WORKER_ID})
    return pika.BlockingConnection(params)


def process(channel, method, properties, body):
    started_at = datetime.now(timezone.utc)
    try:
        job = json.loads(body)
        job_id = job["jobId"]
    except (ValueError, KeyError, TypeError):
        log.error("Malformed job message, sending to DLQ")
        channel.basic_nack(method.delivery_tag, requeue=False)  # permanent: retry won't help
        return

    info("Processing job", jobId=job_id, correlationId=job.get("correlationId"))
    t0 = time.perf_counter()
    try:
        out = run_ocr(job)
        result = {
            "jobId": job_id,
            "correlationId": job.get("correlationId"),
            "ocrOutcome": out.outcome,
            "extractedFields": out.fields,
            "confidence": out.confidence,
            "processingMs": int((time.perf_counter() - t0) * 1000),
            "startedAt": started_at.isoformat(),
            "modelVersion": MODEL_VERSION,
        }
        channel.basic_publish(
            exchange="", routing_key=RESULTS_QUEUE, body=json.dumps(result),
            properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent,
                                            content_type="application/json",
                                            message_id=job_id, correlation_id=job.get("correlationId")),
            mandatory=True)  # confirm_delivery is on: raises if the broker doesn't confirm
    except Exception:
        log.exception("Job failed, requeueing", extra={"extra_fields": {"jobId": job_id}})
        time.sleep(3)  # avoid a hot redelivery loop
        channel.basic_nack(method.delivery_tag, requeue=True)
        return

    channel.basic_ack(method.delivery_tag)
    info("Job done", jobId=job_id, outcome=out.outcome, ms=result["processingMs"])


def main():
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True
        log.info("Shutdown requested, finishing current job")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    while not stop:
        try:
            conn = connect()
            channel = conn.channel()
            channel.confirm_delivery()
            declare_queue(channel, JOBS_QUEUE)
            declare_queue(channel, RESULTS_QUEUE)
            channel.basic_qos(prefetch_count=1)  # fair dispatch
            info("Connected, waiting for jobs", queue=JOBS_QUEUE)
            for method, props, body in channel.consume(JOBS_QUEUE, auto_ack=False, inactivity_timeout=1):
                if stop:
                    break
                if method is None:
                    continue
                process(channel, method, props, body)
            channel.cancel()
            conn.close()
        except pika.exceptions.AMQPConnectionError:
            log.warning("RabbitMQ unavailable, retrying in 5s")
            time.sleep(5)
    log.info("Stopped")


if __name__ == "__main__":
    main()
