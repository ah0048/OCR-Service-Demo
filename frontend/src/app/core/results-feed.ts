import { computed, signal } from '@angular/core';
import { DashboardStore } from './dashboard-store';
import { LiveResult } from './models';
import { ResultsStore } from './results-store';

const PAGE = 30;

/**
 * The view state shared by the live result pages (technical and business): pause / resume,
 * "show more", an optional outcome filter, and which rows just arrived (for the entrance animation).
 * It is a plain class, not an Angular service: each page creates its own, so pausing one page
 * never freezes the other.
 */
export class ResultsFeed {
  readonly filter = signal('');
  readonly limit = signal(PAGE);

  /** while paused the list is frozen so rows stop moving under the cursor; new results are only counted */
  private frozen = signal<LiveResult[] | null>(null);
  private receivedAtPause = 0;

  readonly paused = computed(() => this.frozen() !== null);
  readonly newWhilePaused = computed(() => (this.frozen() ? this.store.received() - this.receivedAtPause : 0));

  readonly shown = computed(() => this.frozen() ?? this.store.results());
  readonly counts = computed(() => {
    const c: Record<string, number> = {};
    for (const r of this.shown()) c[r.outcome] = (c[r.outcome] ?? 0) + 1;
    return c;
  });
  private filtered = computed(() => {
    const f = this.filter();
    return f ? this.shown().filter(r => r.outcome === f) : this.shown();
  });
  readonly visible = computed(() => this.filtered().slice(0, this.limit()));
  readonly hidden = computed(() => Math.max(0, this.filtered().length - this.limit()));

  /** refreshes the "x seconds ago" labels and the "fresh" flags once per second */
  readonly now = computed(() => { this.dashboard.tick(); return Date.now(); });

  /** only results that just arrived live get the entrance animation (not the first page load, not filter changes) */
  readonly isFresh = (r: LiveResult) => r.live && this.now() - r.arrivedAt < 2500;

  constructor(readonly store: ResultsStore, private dashboard: DashboardStore) {}

  togglePause(): void {
    if (this.frozen()) { this.frozen.set(null); return; }
    this.receivedAtPause = this.store.received();
    this.frozen.set(this.store.results());
  }

  showMore(): void { this.limit.update(n => n + PAGE); }
}
