import { Component, inject } from '@angular/core';
import { ImagePlaceholder } from '../../components/image-placeholder/image-placeholder';
import { JsonView } from '../../components/json-view/json-view';
import { DashboardStore } from '../../core/dashboard-store';
import { ago, duration, workerPid } from '../../core/fmt';
import { OUTCOME_CHIP, OUTCOME_COLOR, OUTCOME_LABEL, ResultItem } from '../../core/models';
import { ResultsFeed } from '../../core/results-feed';
import { ResultsStore } from '../../core/results-store';

@Component({
  selector: 'app-results',
  imports: [ImagePlaceholder, JsonView],
  templateUrl: './results.html',
  styleUrl: './results.scss',
})
export class Results {
  private dashboard = inject(DashboardStore);
  private feed = new ResultsFeed(inject(ResultsStore), this.dashboard);

  protected results = this.feed.store;
  protected outcomes = Object.keys(OUTCOME_LABEL).filter(o => o !== 'TechnicalError');
  protected label = OUTCOME_LABEL;
  protected chip = OUTCOME_CHIP;
  protected color = OUTCOME_COLOR;
  protected pid = workerPid;
  protected dur = duration;
  protected ago = ago;

  // the shared feed state, under the names the template uses
  protected outcomeFilter = this.feed.filter;
  protected limit = this.feed.limit;
  protected paused = this.feed.paused;
  protected newWhilePaused = this.feed.newWhilePaused;
  protected counts = this.feed.counts;
  protected visible = this.feed.visible;
  protected hidden = this.feed.hidden;
  protected now = this.feed.now;
  protected isFresh = this.feed.isFresh;
  protected togglePause(): void { this.feed.togglePause(); }

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

  protected copy(text: string, what: string): void {
    navigator.clipboard?.writeText(text);
    this.dashboard.toast('info', `${what} copied`);
  }

  protected open(r: ResultItem): void { this.dashboard.openJob(r.jobId); }
}
