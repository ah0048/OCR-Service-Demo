import { Component, computed, input } from '@angular/core';

export interface Slice { label: string; value: number; color: string; }

@Component({
  selector: 'app-donut',
  templateUrl: './donut.html',
  styleUrl: './donut.scss',
})
export class Donut {
  slices = input.required<Slice[]>();
  centerValue = input('');
  centerLabel = input('');
  arcs = computed(() => {
    const list = this.slices();
    const total = list.reduce((a, s) => a + s.value, 0);
    const c = 2 * Math.PI * 46;
    let acc = 0;
    return list.filter(s => s.value > 0).map(s => {
      const len = total > 0 ? (s.value / total) * c : 0;
      const arc = { label: s.label, color: s.color, dash: `${Math.max(0, len - 1)} ${c - Math.max(0, len - 1)}`, offset: -acc };
      acc += len;
      return arc;
    });
  });
}
