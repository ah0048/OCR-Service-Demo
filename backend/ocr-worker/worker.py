"""OCR worker: consumes `ocr-jobs`, runs inference, publishes to `ocr-results`.

Bounded prefetch and job threads; shared models retain one document-inference slot.
The job is acked only after its result is published with publisher confirms.
"""
import json
import logging
import os
import signal
import socket
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import pika

from inference import IMAGE_API_URL, MODEL_VERSION, OcrOutput, initialize, run_ocr

RABBIT_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBIT_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBIT_USER = os.getenv("RABBITMQ_USER", "ocr")
RABBIT_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "ocr_dev_pw")
JOBS_QUEUE = "ocr-jobs"
RESULTS_QUEUE = "ocr-results"
EVENTS_QUEUE = "job-events"
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


def publish_started(channel, job_id, started_at):
    """Best-effort 'OCR started' event for the dashboard; never fails the job."""
    try:
        channel.basic_publish(
            exchange="", routing_key=EVENTS_QUEUE,
            body=json.dumps({"jobId": job_id, "workerId": WORKER_ID, "startedAt": started_at.isoformat()}),
            properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent, content_type="application/json"))
    except Exception:
        log.warning("Could not publish job-started event", extra={"extra_fields": {"jobId": job_id}})


def job_concurrency():
    try:
        value = int(os.getenv('OCR_JOB_CONCURRENCY', '1'))
    except ValueError:
        raise ValueError('OCR_JOB_CONCURRENCY must be an integer from 1 to 8') from None
    if not 1 <= value <= 8:
        raise ValueError('OCR_JOB_CONCURRENCY must be an integer from 1 to 8')
    return value


@dataclass
class PendingJob:
    future: Future
    delivery_tag: int
    job: dict
    started_at: datetime
    timer: float


def submit_job(channel, method, body, executor):
    """Validate and submit without waiting; only the consumer thread calls Pika."""
    started_at = datetime.now(timezone.utc)
    try:
        job = json.loads(body)
        from uuid import UUID
        job_id = str(UUID(job["jobId"]))
        UUID(job["correlationId"])
        if not isinstance(job.get("imageReference"), str):
            raise ValueError("Missing image reference")
    except (ValueError, KeyError, TypeError, AttributeError):
        log.error("Malformed job message, sending to DLQ")
        channel.basic_nack(method.delivery_tag, requeue=False)  # permanent: retry won't help
        return

    job['jobId'] = job_id
    info("Processing job", jobId=job_id, correlationId=job.get("correlationId"))
    publish_started(channel, job_id, started_at)
    t0 = time.perf_counter()
    return PendingJob(executor.submit(run_ocr, job), method.delivery_tag, job, started_at, t0)


def finish_job(channel, pending):
    """Publish and acknowledge one completed future on its original channel."""
    job = pending.job
    job_id = str(job['jobId'])
    try:
        try:
            out = pending.future.result()
        except Exception:
            log.exception("Inference failed", extra={"extra_fields": {"jobId": job_id}})
            out = OcrOutput("TechnicalError", details={"errorCode": "INFERENCE_ERROR"})
        result = {
            "jobId": job_id,
            "correlationId": job.get("correlationId"),
            "ocrOutcome": out.outcome,
            "extractedFields": out.fields,
            "details": out.details,
            "confidence": out.confidence,
            "processingMs": int((time.perf_counter() - pending.timer) * 1000),
            "startedAt": pending.started_at.isoformat(),
            "modelVersion": MODEL_VERSION,
            "workerId": WORKER_ID,
        }
        channel.basic_publish(
            exchange="", routing_key=RESULTS_QUEUE, body=json.dumps(result, ensure_ascii=False).encode('utf-8'),
            properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent,
                                            content_type="application/json",
                                            message_id=job_id, correlation_id=job.get("correlationId")),
            mandatory=True)  # confirm_delivery is on: raises if the broker doesn't confirm
    except Exception:
        log.exception("Result publication failed", extra={"extra_fields": {"jobId": job_id}})
        raise  # Connection closes; RabbitMQ redelivers the unacked job.

    channel.basic_ack(pending.delivery_tag)
    info("Job done", jobId=job_id, outcome=out.outcome, ms=result["processingMs"])


def consume_jobs(channel, executor, concurrency, should_stop):
    """Bound accepted work and keep completions/heartbeats on this thread."""
    channel.basic_qos(prefetch_count=concurrency)
    pending = []

    def finish_ready():
        for task in pending[:]:
            if task.future.done():
                finish_job(channel, task)
                pending.remove(task)

    deliveries = channel.consume(JOBS_QUEUE, auto_ack=False, inactivity_timeout=0.1)
    try:
        while not should_stop():
            finish_ready()
            if len(pending) >= concurrency:
                channel.connection.process_data_events(time_limit=0.1)
                continue
            try:
                method, props, body = next(deliveries)
            except StopIteration:
                break
            if should_stop():
                break  # Delivered but unstarted: requeued when the connection closes.
            if method is not None:
                task = submit_job(channel, method, body, executor)
                if task is not None:
                    pending.append(task)
        # Graceful stop: accept no more jobs, finish all already-submitted work.
        while pending:
            finish_ready()
            if pending:
                channel.connection.process_data_events(time_limit=0.1)
    except BaseException:
        # Close before waiting for running inference so unacked deliveries can
        # be redelivered. These futures must never publish via a new channel.
        try:
            if channel.connection.is_open:
                channel.connection.close()
        except Exception:
            log.warning('Could not close failed RabbitMQ connection')
        raise
    finally:
        # Cancel queued work and finish running calls before any reconnect.
        # Nothing is submitted to the next connection until this pool is clear.
        for task in pending:
            task.future.cancel()
        for task in pending:
            try:
                task.future.result()
            except Exception:
                pass


def main():
    concurrency = job_concurrency()
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True
        log.info("Shutdown requested, finishing accepted jobs")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    info("Loading offline OCR models")
    initialize()
    info("OCR models ready", imageSource="api" if IMAGE_API_URL else "filesystem",
         device=os.getenv("OCR_DEVICE", "cpu"), jobConcurrency=concurrency, inferenceSlots=1)
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ocr-inference")
    while not stop:
        conn = None
        try:
            conn = connect()
            channel = conn.channel()
            channel.confirm_delivery()
            declare_queue(channel, JOBS_QUEUE)
            declare_queue(channel, RESULTS_QUEUE)
            declare_queue(channel, EVENTS_QUEUE)
            info("Connected, waiting for jobs", queue=JOBS_QUEUE)
            consume_jobs(channel, executor, concurrency, lambda: stop)
            channel.cancel()
            conn.close()
        except (pika.exceptions.AMQPError, OSError):
            log.warning("RabbitMQ unavailable, retrying in 5s")
            time.sleep(5)
        finally:
            if conn is not None and conn.is_open:
                conn.close()
    executor.shutdown(wait=True)
    log.info("Stopped")


if __name__ == "__main__":
    main()
