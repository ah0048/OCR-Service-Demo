export type JobStatus = 'Queued' | 'OcrInProgress' | 'Completed';
export type JobOutcome = 'Succeeded' | 'ValidationFailed' | 'ManualReview' | 'OcrFailed' | 'TechnicalError';

export interface QueueStat {
  name: string;
  role: 'jobs' | 'results' | 'events' | 'dead-letter';
  ready: number;
  unacked: number;
  total: number;
  consumers: number;
  publishRate: number;
  deliverRate: number;
  ackRate: number;
  published: number;
  acked: number;
  redelivered: number;
}

export interface Worker {
  id: string;
  online: boolean;
  busy: boolean;
  prefetch: number;
  processed: number;
  lastActivity: string | null;
}

export interface WorkerGroup {
  kind: 'ocr' | 'business';
  onlineCount: number;
  workers: Worker[];
}

export interface BatchSummary {
  id: string;
  name: string | null;
  total: number;
  queued: number;
  inProgress: number;
  completed: number;
  succeeded: number;
  validationFailed: number;
  manualReview: number;
  ocrFailed: number;
  createdAt: string;
  lastCompletedAt: string | null;
}

export interface Snapshot {
  serverTime: string;
  rabbitAvailable: boolean;
  totals: { total: number; queued: number; inProgress: number; completed: number; byOutcome: Record<string, number> };
  throughput: { perSecond10s: number; perSecond60s: number };
  latency: { sampleSize: number; avgEndToEndMs: number; p95EndToEndMs: number; avgOcrMs: number };
  queues: QueueStat[];
  ocrWorkers: WorkerGroup;
  businessWorkers: WorkerGroup;
  batches: BatchSummary[];
}

export interface JobRow {
  jobId: string;
  batchId: string | null;
  batchName: string | null;
  clientReference: string;
  imageReference: string;
  status: JobStatus;
  outcome: JobOutcome | null;
  reasonCodes: string[];
  ocrOutcome: string | null;
  confidence: number | null;
  ocrDurationMs: number | null;
  ocrWorkerId: string | null;
  businessWorkerId: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
  resultDeliveries: number;
}

export interface JobPage { total: number; page: number; pageSize: number; items: JobRow[]; }

export interface JobDetail extends JobRow {
  correlationId: string;
  ocr: { outcome: string; confidence: number; durationMs: number; modelVersion: string; fields: Record<string, string> | null } | null;
}

export interface JobFilter {
  page: number;
  pageSize: number;
  status: string;
  outcome: string;
  search: string;
  batchId: string;
}

export const OUTCOME_COLOR: Record<string, string> = {
  Succeeded: '#1f9d6b',
  ValidationFailed: '#dc4350',
  ManualReview: '#e39a1b',
  OcrFailed: '#7c5cdb',
  TechnicalError: '#64748b',
};

export const OUTCOME_CHIP: Record<string, string> = {
  Succeeded: 'ok',
  ValidationFailed: 'bad',
  ManualReview: 'warn',
  OcrFailed: 'violet',
  TechnicalError: 'slate',
};

export const OUTCOME_LABEL: Record<string, string> = {
  Succeeded: 'Succeeded',
  ValidationFailed: 'Validation failed',
  ManualReview: 'Manual review',
  OcrFailed: 'OCR failed',
  TechnicalError: 'Technical error',
};

/** A result as kept by the client: `live` is false for the initial backlog, `arrivedAt` is when it reached the browser. */
export type LiveResult = ResultItem & { live: boolean; arrivedAt: number };

/** One finished OCR result, pushed by /api/results/stream. */
export interface ResultItem {
  jobId: string;
  clientReference: string;
  imageReference: string;
  batchName: string | null;
  outcome: JobOutcome;
  reasonCodes: string[];
  ocr: { outcome: string; confidence: number; durationMs: number; modelVersion: string; fields: Record<string, string> | null };
  ocrWorkerId: string | null;
  businessWorkerId: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string;
}
