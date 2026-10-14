import { Component, computed, input } from '@angular/core';

export interface Seg { value: number; color: string; label?: string; striped?: boolean; }

@Component({
  selector: 'app-seg-bar',
  templateUrl: './seg-bar.html',
})
export class SegBar {
  segments = input.required<Seg[]>();
  total = input<number | null>(null);
  size = input<'sm' | 'md' | 'lg'>('md');
  segs = computed(() => {
    const list = this.segments();
    const total = this.total() ?? list.reduce((a, s) => a + s.value, 0);
    return list.map(s => ({ ...s, pct: total > 0 ? (s.value / total) * 100 : 0 }));
  });
}
