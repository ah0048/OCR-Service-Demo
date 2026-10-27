import { Component, computed, inject, input } from '@angular/core';
import { Router } from '@angular/router';
import { DashboardStore } from '../../core/dashboard-store';
import { ago, between, duration, eta, pct, shortId, utc } from '../../core/fmt';
import { BatchSummary, OUTCOME_COLOR } from '../../core/models';
import { Seg, SegBar } from '../seg-bar/seg-bar';

@Component({
  selector: 'app-batch-card',
  imports: [SegBar],
  templateUrl: './batch-card.html',
  styleUrl: './batch-card.scss',
})
export class BatchCard {
  private store = inject(DashboardStore);
  private router = inject(Router);
  batch = input.required<BatchSummary>();
  protected ago = ago;

  total = computed(() => this.batch().total);
  id = computed(() => shortId(this.batch().id));
  isDone = computed(() => this.batch().completed >= this.batch().total);
  percent = computed(() => pct(this.batch().completed, this.batch().total));
  took = computed(() => duration(between(this.batch().createdAt, this.batch().lastCompletedAt)));

  /** ticks with every live snapshot, so the elapsed time keeps counting while the batch is processing */
  private now = computed(() => { this.store.tick(); return Date.now(); });
  elapsedMs = computed(() => {
    const b = this.batch();
    return this.isDone()
      ? between(b.createdAt, b.lastCompletedAt) ?? 0
      : Math.max(0, this.now() - new Date(utc(b.createdAt)).getTime());
  });
  totalTime = computed(() => duration(this.elapsedMs()));
  /** elapsed so far + ETA; only meaningful once the batch has a measurable speed */
  estimatedTotal = computed(() => {
    const etaSec = this.store.batchRates()[this.batch().id]?.etaSec;
    return etaSec == null ? '—' : duration(this.elapsedMs() + etaSec * 1000);
  });
  rate = computed(() => this.store.batchRates()[this.batch().id]?.rate ?? 0);
  etaText = computed(() => eta(this.store.batchRates()[this.batch().id]?.etaSec));
  successRate = computed(() => pct(this.batch().succeeded, this.batch().completed));

  segments = computed<Seg[]>(() => {
    const b = this.batch();
    return [
      { value: b.rejected, color: OUTCOME_COLOR['Rejected'], label: 'Rejected' },
      { value: b.succeeded, color: OUTCOME_COLOR['Succeeded'], label: 'Succeeded' },
      { value: b.manualReview, color: OUTCOME_COLOR['ManualReview'], label: 'Manual review' },
      { value: b.validationFailed, color: OUTCOME_COLOR['ValidationFailed'], label: 'Validation failed' },
      { value: b.technicalError, color: OUTCOME_COLOR['TechnicalError'], label: 'Technical error' },
      { value: b.ocrFailed, color: OUTCOME_COLOR['OcrFailed'], label: 'OCR failed' },
      { value: b.inProgress, color: '#3b82f6', label: 'In OCR / validation', striped: true },
      { value: b.queued, color: '#c3cad8', label: 'Waiting' },
    ];
  });

  viewJobs(): void { this.router.navigate(['/jobs'], { queryParams: { batch: this.batch().id } }); }
}
