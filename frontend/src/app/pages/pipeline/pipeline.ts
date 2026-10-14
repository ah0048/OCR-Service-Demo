import { Component, computed, inject, signal } from '@angular/core';
import { DashboardStore } from '../../core/dashboard-store';
import { ago, compact, pct, workerHost, workerPid } from '../../core/fmt';
import { QueueStat, Worker, WorkerGroup } from '../../core/models';
import { SegBar } from '../../components/seg-bar/seg-bar';

const DESCRIPTIONS: Record<string, string> = {
  'ocr-jobs': 'Jobs waiting for OCR. Consumed by the Python workers.',
  'ocr-results': 'OCR output waiting for business rules. Consumed by the .NET workers.',
  'job-events': '"OCR started" notifications that move a job to the in-progress state.',
  'ocr-jobs.dlq': 'Dead letters: malformed jobs rejected by an OCR worker.',
  'ocr-results.dlq': 'Dead letters: malformed results or unknown job ids.',
  'job-events.dlq': 'Dead letters from the events queue.',
};

@Component({
  selector: 'app-pipeline',
  imports: [SegBar],
  templateUrl: './pipeline.html',
  styleUrl: './pipeline.scss',
})
export class Pipeline {
  private store = inject(DashboardStore);
  protected snap = this.store.snapshot;
  protected showOffline = signal(false);

  protected queues = this.store.queues;
  protected mainQueues = computed(() => this.queues().filter(q => q.role === 'jobs' || q.role === 'results' || q.role === 'events'));

  protected compact = compact;
  protected pct = pct;
  protected ago = ago;
  protected pid = workerPid;
  protected host = workerHost;
  protected desc = (n: string) => DESCRIPTIONS[n] ?? '';
  protected roleChip = (q: QueueStat) => (q.role === 'dead-letter' ? (q.total > 0 ? 'bad' : 'slate') : q.role === 'events' ? 'violet' : 'info');
  protected visible = (g: WorkerGroup): Worker[] => g.workers.filter(w => w.online || this.showOffline());
  protected total = (g: WorkerGroup) => g.workers.reduce((a, w) => a + w.processed, 0);
}
