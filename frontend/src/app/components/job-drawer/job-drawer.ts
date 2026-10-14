import { Component, HostListener, computed, effect, inject, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { Api } from '../../core/api';
import { DashboardStore } from '../../core/dashboard-store';
import { between, duration, shortId, utc, workerHost, workerPid } from '../../core/fmt';
import { JobDetail, OUTCOME_COLOR } from '../../core/models';
import { Seg, SegBar } from '../seg-bar/seg-bar';
import { StateChip } from '../state-chip/state-chip';

const REASONS: Record<string, string> = {
  OCR_UNREADABLE: 'The OCR engine could not read the image.',
  LOW_CONFIDENCE: 'OCR confidence is below the 80% threshold, so a person should review it.',
  NAME_MISSING: 'The full name was not extracted.',
  BIRTHDATE_INVALID: 'The birth date is missing, malformed or in the future.',
  NATIONAL_ID_FORMAT: 'The national ID must be exactly 14 digits.',
  NATIONAL_ID_BIRTHDATE_MISMATCH: 'The date encoded in the national ID does not match the birth date.',
  EXPIRY_INVALID: 'The expiry date is missing or malformed.',
  ID_EXPIRED: 'The ID card has expired.',
};

@Component({
  selector: 'app-job-drawer',
  imports: [SegBar, StateChip],
  templateUrl: './job-drawer.html',
  styleUrl: './job-drawer.scss',
})
export class JobDrawer {
  protected store = inject(DashboardStore);
  private api = inject(Api);
  private router = inject(Router);

  protected job = signal<JobDetail | null>(null);
  protected missing = signal(false);
  protected short = shortId;
  protected pid = workerPid;
  protected host = workerHost;
  protected dur = duration;
  private seq = 0;

  private now = computed(() => { this.store.tick(); return Date.now(); });

  constructor() {
    // load on open; keep refreshing on every live tick until the job completes
    effect(() => {
      const id = this.store.selectedJobId();
      this.store.tick();
      untracked(() => {
        if (!id) { this.job.set(null); this.missing.set(false); return; }
        if (this.job()?.jobId !== id) { this.job.set(null); this.missing.set(false); }
        else if (this.job()?.status === 'Completed') return;
        const seq = ++this.seq;
        this.api.getJob(id).subscribe({
          next: j => { if (seq === this.seq) this.job.set(j); },
          error: e => { if (e.status === 404) this.missing.set(true); },
        });
      });
    });
  }

  @HostListener('document:keydown.escape') close() { this.store.openJob(null); }

  protected keys = (o: Record<string, string>) => Object.keys(o);
  protected reasonText = (c: string) => REASONS[c] ?? c;
  protected raw = computed(() => JSON.stringify(this.job(), null, 2));
  protected time = (iso: string | null) => (iso ? new Date(utc(iso)).toLocaleTimeString([], { hour12: false }) + '.' + String(new Date(utc(iso)).getMilliseconds()).padStart(3, '0') : '—');

  protected totalMs = computed(() => between(this.job()?.createdAt, this.job()?.completedAt) ?? 0);

  protected steps = computed(() => {
    const j = this.job();
    if (!j) return [];
    const st = j.status;
    const wait = between(j.createdAt, j.startedAt);
    return [
      { label: 'Submitted', sub: 'saved + queued', state: 'done' },
      { label: 'OCR queue', sub: st === 'Queued' ? 'waiting for a worker' : wait !== null ? `waited ${duration(wait)}` : '', state: st === 'Queued' ? 'active' : 'done' },
      { label: 'OCR', sub: st === 'OcrInProgress' ? 'extracting…' : j.ocr ? duration(j.ocr.durationMs) : '', state: st === 'OcrInProgress' ? 'active' : st === 'Completed' ? 'done' : 'todo' },
      { label: 'Validation', sub: st === 'Completed' ? 'rules applied' : '', state: st === 'Completed' ? 'done' : 'todo' },
    ];
  });

  protected progress = computed(() => {
    const j = this.job();
    if (!j) return 0;
    if (j.status === 'Completed') return 100;
    if (j.status === 'Queued') return 12;
    // OCR in progress: estimate from elapsed time vs the average OCR duration
    const expected = this.store.snapshot()?.latency.avgOcrMs || 2000;
    const elapsed = this.now() - new Date(utc(j.startedAt ?? j.createdAt)).getTime();
    return Math.min(92, 25 + (Math.max(0, elapsed) / expected) * 55);
  });

  protected progressColor = computed(() => {
    const j = this.job();
    return j?.status === 'Completed' ? OUTCOME_COLOR[j.outcome ?? ''] ?? '#1f9d6b' : j?.status === 'OcrInProgress' ? '#2f6fed' : '#94a0b8';
  });

  protected progressLabel = computed(() => {
    const j = this.job();
    return j?.status === 'Completed' ? 'Done' : j?.status === 'OcrInProgress' ? 'OCR in progress (estimated)' : 'Waiting in the ocr-jobs queue';
  });

  protected liveNote = computed(() => {
    const j = this.job();
    if (j?.status === 'OcrInProgress')
      return `A Python worker (${workerPid(j.ocrWorkerId)}) picked this job up and is running OCR. When it publishes the result, a .NET business worker validates it and this panel will update by itself.`;
    return 'The job is saved in PostgreSQL and sits in the ocr-jobs queue until an OCR worker is free. Workers take one job at a time, so a deep queue drains at (workers ÷ OCR time) jobs per second.';
  });

  protected timeSegs = computed<Seg[]>(() => {
    const j = this.job();
    if (!j || j.status !== 'Completed') return [];
    const total = this.totalMs();
    const wait = Math.max(0, between(j.createdAt, j.startedAt) ?? 0);
    const ocr = j.ocr?.durationMs ?? 0;
    const post = Math.max(0, total - wait - ocr);
    return [
      { value: wait, color: '#c3cad8', label: 'Queue wait' },
      { value: ocr, color: '#2f6fed', label: 'OCR' },
      { value: post, color: '#7c5cdb', label: 'Result queue + validation' },
    ];
  });

  openBatch(id: string | null): void {
    if (!id) return;
    this.close();
    this.router.navigate(['/jobs'], { queryParams: { batch: id } });
  }

  copy(text: string): void { navigator.clipboard?.writeText(text); this.store.toast('info', 'Job id copied'); }
}
