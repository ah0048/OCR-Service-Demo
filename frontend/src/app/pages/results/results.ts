import { Component, computed, inject, signal } from '@angular/core';
import { ImagePlaceholder } from '../../components/image-placeholder/image-placeholder';
import { JsonView } from '../../components/json-view/json-view';
import { DashboardStore } from '../../core/dashboard-store';
import { ago, duration, workerPid } from '../../core/fmt';
import { LiveResult, OUTCOME_CHIP, OUTCOME_COLOR, OUTCOME_LABEL, ResultItem } from '../../core/models';
import { ResultsStore } from '../../core/results-store';

@Component({
  selector: 'app-results',
  imports: [ImagePlaceholder, JsonView],
  templateUrl: './results.html',
  styleUrl: './results.scss',
})
export class Results {
  protected results = inject(ResultsStore);
  private dashboard = inject(DashboardStore);

  protected outcomes = Object.keys(OUTCOME_LABEL).filter(o => o !== 'TechnicalError');
  protected label = OUTCOME_LABEL;
  protected chip = OUTCOME_CHIP;
  protected color = OUTCOME_COLOR;
  protected pid = workerPid;
  protected dur = duration;

  protected outcomeFilter = signal('');
  protected limit = signal(30);

  /** while paused the list is frozen so rows stop moving under the cursor; new results are only counted */
  private frozen = signal<LiveResult[] | null>(null);
  private receivedAtPause = 0;
  protected paused = computed(() => this.frozen() !== null);
  protected newWhilePaused = computed(() => (this.frozen() ? this.results.received() - this.receivedAtPause : 0));

  private shown = computed(() => this.frozen() ?? this.results.results());
  protected counts = computed(() => {
    const c: Record<string, number> = {};
    for (const r of this.shown()) c[r.outcome] = (c[r.outcome] ?? 0) + 1;
    return c;
  });
  private filtered = computed(() => {
    const f = this.outcomeFilter();
    return f ? this.shown().filter(r => r.outcome === f) : this.shown();
  });
  protected visible = computed(() => this.filtered().slice(0, this.limit()));
  protected hidden = computed(() => Math.max(0, this.filtered().length - this.limit()));

  /** refreshes the "x seconds ago" labels once per second */
  protected now = computed(() => { this.dashboard.tick(); return Date.now(); });
  protected ago = ago;

  /** only results that just arrived live get the entrance animation (not the first page load, not filter changes) */
  protected isFresh = (r: LiveResult) => r.live && this.now() - r.arrivedAt < 2500;

  protected confColor = (c: number) => (c >= 0.8 ? '#1f9d6b' : c >= 0.5 ? '#e39a1b' : '#dc4350');

  /** the OCR output as the worker produced it */
  protected jsonFor(r: ResultItem) {
    return {
      jobId: r.jobId,
      imageReference: r.imageReference,
      ocrOutcome: r.ocr.outcome,
      confidence: r.ocr.confidence,
      extractedFields: r.ocr.fields,
      processingMs: r.ocr.durationMs,
      modelVersion: r.ocr.modelVersion,
    };
  }

  protected togglePause(): void {
    if (this.frozen()) { this.frozen.set(null); return; }
    this.receivedAtPause = this.results.received();
    this.frozen.set(this.results.results());
  }

  protected copy(text: string, what: string): void {
    navigator.clipboard?.writeText(text);
    this.dashboard.toast('info', `${what} copied`);
  }

  protected open(r: ResultItem): void { this.dashboard.openJob(r.jobId); }
}
