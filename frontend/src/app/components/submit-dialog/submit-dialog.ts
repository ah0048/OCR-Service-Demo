import { Component, HostListener, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Api } from '../../core/api';
import { DashboardStore } from '../../core/dashboard-store';

@Component({
  selector: 'app-submit-dialog',
  templateUrl: './submit-dialog.html',
  styleUrl: './submit-dialog.scss',
})
export class SubmitDialog {
  protected store = inject(DashboardStore);
  private api = inject(Api);
  private router = inject(Router);

  protected mode = signal<'single' | 'batch'>('single');
  protected busy = signal(false);
  protected result = signal<{ title: string; text: string; bad?: boolean } | null>(null);

  protected clientRef = signal('CLIENT-' + Math.floor(1000 + Math.random() * 9000));
  protected imageRef = signal('national-id-' + Math.floor(1000 + Math.random() * 9000) + '.jpg');
  protected count = signal(100);
  protected prefix = signal('BATCH');

  @HostListener('document:keydown.escape') close() { this.store.submitOpen.set(false); this.result.set(null); }

  sendSingle(): void {
    const t0 = performance.now();
    this.busy.set(true);
    this.api.submitJob(this.clientRef().trim(), this.imageRef().trim()).subscribe({
      next: r => {
        const ms = Math.round(performance.now() - t0);
        this.busy.set(false);
        this.store.toast('ok', `202 Accepted in ${ms} ms. Following the job…`);
        this.clientRef.set('CLIENT-' + Math.floor(1000 + Math.random() * 9000));
        this.imageRef.set('national-id-' + Math.floor(1000 + Math.random() * 9000) + '.jpg');
        this.close();
        this.store.openJob(r.jobId);
      },
      error: e => this.fail(e),
    });
  }

  sendBatch(): void {
    const t0 = performance.now();
    const n = this.count();
    this.busy.set(true);
    this.api.submitBatch(n, this.prefix().trim() || 'BATCH').subscribe({
      next: () => {
        const ms = Math.round(performance.now() - t0);
        this.busy.set(false);
        this.store.toast('ok', `${n.toLocaleString()} jobs accepted (202) in ${ms} ms. OCR runs in the background.`);
        this.close();
        this.router.navigate(['/batches']);
      },
      error: e => this.fail(e),
    });
  }

  private fail(e: { status?: number; error?: { title?: string } }): void {
    this.busy.set(false);
    this.result.set({ bad: true, title: `Request failed${e.status ? ' (' + e.status + ')' : ''}`, text: e.error?.title ?? 'Is the API running on http://localhost:5125?' });
  }
}
