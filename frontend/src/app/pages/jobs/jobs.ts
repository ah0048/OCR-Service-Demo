import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { Api } from '../../core/api';
import { DashboardStore } from '../../core/dashboard-store';
import { JobsTable } from '../../components/jobs-table/jobs-table';
import { shortId } from '../../core/fmt';
import { JobPage, OUTCOME_LABEL } from '../../core/models';

@Component({
  selector: 'app-jobs',
  imports: [JobsTable],
  templateUrl: './jobs.html',
  styleUrl: './jobs.scss',
})
export class Jobs {
  private api = inject(Api);
  private store = inject(DashboardStore);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  protected outcomes = Object.entries(OUTCOME_LABEL);
  protected short = shortId;

  protected data = signal<JobPage | null>(null);
  protected page = signal(1);
  protected pageSize = signal(25);
  protected status = signal('');
  protected outcome = signal('');
  protected search = signal('');
  protected paused = signal(false);

  private params = toSignal(this.route.queryParamMap);
  protected batchId = computed(() => this.params()?.get('batch') ?? '');
  protected pages = computed(() => Math.max(1, Math.ceil((this.data()?.total ?? 0) / this.pageSize())));

  private requestSeq = 0;
  private searchTimer?: ReturnType<typeof setTimeout>;

  constructor() {
    effect(() => {
      // re-run when a filter changes, or (unless paused) on each live tick
      const filter = { page: this.page(), pageSize: this.pageSize(), status: this.status(), outcome: this.outcome(), search: this.search(), batchId: this.batchId() };
      if (!this.paused()) this.store.tick();
      untracked(() => {
        const seq = ++this.requestSeq;
        this.api.listJobs(filter).subscribe({
          next: p => { if (seq === this.requestSeq) this.data.set(p); },
          error: () => {},
        });
      });
    });
  }

  go(p: number): void { this.page.set(Math.min(Math.max(1, p), this.pages())); }
  setStatus(v: string): void { this.status.set(v); this.page.set(1); }
  setOutcome(v: string): void { this.outcome.set(v); this.page.set(1); }
  setPageSize(n: number): void { this.pageSize.set(n); this.page.set(1); }
  setSearch(v: string): void {
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => { this.search.set(v); this.page.set(1); }, 250);
  }
  clearBatch(): void { this.router.navigate([], { queryParams: { batch: null }, queryParamsHandling: 'merge' }); this.page.set(1); }
}
