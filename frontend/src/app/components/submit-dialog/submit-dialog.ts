import { Component, HostListener, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Api } from '../../core/api';
import { DashboardStore } from '../../core/dashboard-store';

interface Pair { reference: string; front: File; back: File; }

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
  protected prefix = signal('BATCH');
  protected front = signal<File | null>(null);
  protected back = signal<File | null>(null);
  protected pairs = signal<Pair[]>([]);
  protected pairError = signal('');

  @HostListener('document:keydown.escape')
  close(): void {
    if (this.busy()) return;
    this.store.submitOpen.set(false);
    this.result.set(null);
    this.front.set(null);
    this.back.set(null);
    this.pairs.set([]);
    this.pairError.set('');
  }

  protected choose(event: Event, side: 'front' | 'back'): void {
    const file = (event.target as HTMLInputElement).files?.[0] ?? null;
    (side === 'front' ? this.front : this.back).set(file);
  }

  protected chooseBatch(event: Event): void {
    const files = Array.from((event.target as HTMLInputElement).files ?? []);
    const groups = new Map<string, Partial<Pair>>();
    this.pairs.set([]);
    this.pairError.set('');
    for (const file of files) {
      const match = /^([0-9]+)_([12])\.[^.]+$/.exec(file.name);
      if (!match || !match[1]) {
        this.pairError.set('Use numeric pairs: 3967452_1.jpg (front) and 3967452_2.jpg (back).');
        return;
      }
      const key = match[1].toLowerCase();
      const pair = groups.get(key) ?? { reference: match[1] };
      const side = match[2] === '1' ? 'front' : 'back';
      if (pair[side]) {
        this.pairError.set('Duplicate ' + side + ' image for ' + pair.reference);
        return;
      }
      pair[side] = file;
      groups.set(key, pair);
    }
    if (groups.size > 100 || [...groups.values()].some(p => !p.front || !p.back)) {
      this.pairError.set('Select complete front/back pairs for at most 100 cards.');
      return;
    }
    this.pairs.set([...groups.values()] as Pair[]);
  }

  sendSingle(): void {
    const front = this.front(), back = this.back();
    if (!front || !back) return;
    const t0 = performance.now();
    this.busy.set(true);
    this.api.uploadJob(this.clientRef().trim(), front, back).subscribe({
      next: r => {
        this.busy.set(false);
        this.store.toast('ok', 'Images uploaded and job accepted in ' + Math.round(performance.now() - t0) + ' ms. OCR runs in the background.');
        this.close();
        this.store.openJob(r.jobId);
      },
      error: e => this.fail(e),
    });
  }

  sendBatch(): void {
    const pairs = this.pairs();
    if (!pairs.length) return;
    const t0 = performance.now();
    this.busy.set(true);
    this.api.uploadBatch(this.prefix().trim() || 'BATCH', pairs).subscribe({
      next: () => {
        this.busy.set(false);
        this.store.toast('ok', pairs.length + ' cards uploaded and accepted in ' + Math.round(performance.now() - t0) + ' ms. OCR runs in the background.');
        this.close();
        this.router.navigate(['/batches']);
      },
      error: e => this.fail(e),
    });
  }

  private fail(e: { status?: number; error?: { title?: string; errors?: Record<string, string[]> } }): void {
    this.busy.set(false);
    const validation = Object.values(e.error?.errors ?? {}).flat().join(' ');
    this.result.set({ bad: true, title: 'Request failed' + (e.status ? ' (' + e.status + ')' : ''), text: validation || e.error?.title || 'Is the API running on http://localhost:5125?' });
  }
}
