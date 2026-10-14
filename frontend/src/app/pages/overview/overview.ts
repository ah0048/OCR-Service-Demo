import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Api } from '../../core/api';
import { BatchCard } from '../../components/batch-card/batch-card';
import { DashboardStore } from '../../core/dashboard-store';
import { compact, duration, pct } from '../../core/fmt';
import { JobsTable } from '../../components/jobs-table/jobs-table';
import { JobRow, OUTCOME_COLOR, OUTCOME_LABEL } from '../../core/models';
import { Donut } from '../../components/donut/donut';
import { LineChart, LineSeries } from '../../components/line-chart/line-chart';
import { Seg, SegBar } from '../../components/seg-bar/seg-bar';

@Component({
  selector: 'app-overview',
  imports: [RouterLink, BatchCard, JobsTable, Donut, LineChart, SegBar],
  templateUrl: './overview.html',
  styleUrl: './overview.scss',
})
export class Overview {
  protected store = inject(DashboardStore);
  private api = inject(Api);

  protected snap = this.store.snapshot;
  protected jq = this.store.jobsQueue;
  protected rq = this.store.resultsQueue;
  protected recent = signal<JobRow[]>([]);

  protected compact = compact;
  protected pct = pct;
  protected dur = duration;

  protected tpSeries: LineSeries[] = [{ key: 'throughput', label: 'jobs / s', color: '#e1a82d', area: true }];
  protected queueSeries: LineSeries[] = [
    { key: 'jobsReady', label: 'ocr-jobs ready', color: '#2f6fed', area: true },
    { key: 'resultsReady', label: 'ocr-results ready', color: '#7c5cdb' },
    { key: 'jobsUnacked', label: 'in OCR', color: '#1f9d6b' },
  ];

  protected succeeded = computed(() => this.snap()?.totals.byOutcome['Succeeded'] ?? 0);
  protected inFlight = computed(() => {
    const j = this.jq(), r = this.rq();
    return (j?.ready ?? 0) + (j?.unacked ?? 0) + (r?.ready ?? 0) + (r?.unacked ?? 0);
  });
  protected flightSegs = computed<Seg[]>(() => {
    const j = this.jq(), r = this.rq();
    return [
      { value: j?.ready ?? 0, color: '#c3cad8', label: 'Waiting for OCR' },
      { value: j?.unacked ?? 0, color: '#2f6fed', label: 'In OCR', striped: true },
      { value: (r?.ready ?? 0) + (r?.unacked ?? 0), color: '#7c5cdb', label: 'Awaiting / in validation', striped: true },
    ];
  });
  protected slices = computed(() => {
    const o = this.snap()?.totals.byOutcome ?? {};
    return Object.keys(OUTCOME_LABEL).map(k => ({ label: OUTCOME_LABEL[k], value: o[k] ?? 0, color: OUTCOME_COLOR[k] })).filter(s => s.value > 0 || s.label !== 'Technical error');
  });

  protected busy = (w: { busy: boolean; online: boolean }[]) => w.filter(x => x.online && x.busy).length;

  constructor() {
    effect(() => {
      this.store.tick();
      untracked(() =>
        this.api.listJobs({ page: 1, pageSize: 8, status: '', outcome: '', search: '', batchId: '' }).subscribe({
          next: p => this.recent.set(p.items), error: () => {},
        }));
    });
  }
}
