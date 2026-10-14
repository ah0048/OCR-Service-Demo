import { Component, computed, inject } from '@angular/core';
import { BatchCard } from '../../components/batch-card/batch-card';
import { DashboardStore } from '../../core/dashboard-store';

@Component({
  selector: 'app-batches',
  imports: [BatchCard],
  templateUrl: './batches.html',
  styleUrl: './batches.scss',
})
export class Batches {
  protected store = inject(DashboardStore);
  protected batches = computed(() => this.store.snapshot()?.batches ?? []);
}
