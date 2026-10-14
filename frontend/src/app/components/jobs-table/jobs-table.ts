import { Component, inject, input } from '@angular/core';
import { DashboardStore } from '../../core/dashboard-store';
import { ago, between, duration, shortId, workerPid } from '../../core/fmt';
import { JobRow } from '../../core/models';
import { StateChip } from '../state-chip/state-chip';

@Component({
  selector: 'app-jobs-table',
  imports: [StateChip],
  templateUrl: './jobs-table.html',
  styleUrl: './jobs-table.scss',
})
export class JobsTable {
  protected store = inject(DashboardStore);
  rows = input.required<JobRow[]>();
  compact = input(false);

  protected short = shortId;
  protected pid = workerPid;
  protected ago = ago;
  protected dur = duration;
  protected confColor = (c: number) => (c >= 0.8 ? '#1f9d6b' : c >= 0.5 ? '#e39a1b' : '#dc4350');
  protected total = (j: JobRow) => (j.status === 'Completed' ? duration(between(j.createdAt, j.completedAt)) : '…');
}
