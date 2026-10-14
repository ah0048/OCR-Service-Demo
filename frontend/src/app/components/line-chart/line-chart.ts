import { Component, computed, input, signal } from '@angular/core';
import type { HistoryPoint } from '../../core/dashboard-store';

export interface LineSeries { key: keyof HistoryPoint; label: string; color: string; area?: boolean; }

@Component({
  selector: 'app-line-chart',
  templateUrl: './line-chart.html',
  styleUrl: './line-chart.scss',
})
export class LineChart {
  data = input.required<HistoryPoint[]>();
  series = input.required<LineSeries[]>();
  integer = input(false);
  limit = input(150);

  readonly W = 600; readonly H = 190; readonly padL = 0; readonly padT = 8; readonly padB = 6;
  hover = signal<number | null>(null);

  private max = computed(() => {
    let m = 0;
    for (const p of this.data()) for (const s of this.series()) m = Math.max(m, Number(p[s.key]));
    if (m <= 0) return this.integer() ? 4 : 1;
    const pow = Math.pow(10, Math.floor(Math.log10(m)));
    const n = m / pow;
    const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return Math.max(this.integer() ? 4 : 0.5, nice * pow);
  });

  private x = (i: number, len: number) => this.W - (len - 1 - i) * ((this.W - this.padL) / (this.limit() - 1));
  private y = (v: number) => this.H - this.padB - (v / this.max()) * (this.H - this.padT - this.padB);

  grid = computed(() => [0, 0.5, 1].map(f => ({
    y: this.y(this.max() * f),
    label: this.integer() ? String(Math.round(this.max() * f)) : (this.max() * f).toFixed(this.max() < 5 ? 1 : 0),
  })));

  paths = computed(() => {
    const d = this.data();
    return this.series().map(s => {
      const pts = d.map((p, i) => [this.x(i, d.length), this.y(Number(p[s.key]))] as const);
      const line = this.smooth(pts);
      const base = this.H - this.padB;
      const areaD = pts.length ? `${line} L ${pts[pts.length - 1][0]} ${base} L ${pts[0][0]} ${base} Z` : '';
      return { key: s.key, color: s.color, area: s.area, d: line, areaD };
    });
  });

  private smooth(pts: readonly (readonly [number, number])[]): string {
    if (pts.length === 0) return '';
    if (pts.length === 1) return `M ${pts[0][0]} ${pts[0][1]}`;
    let d = `M ${pts[0][0]} ${pts[0][1]}`;
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      const mx = (x0 + x1) / 2;
      d += ` C ${mx} ${y0}, ${mx} ${y1}, ${x1} ${y1}`;
    }
    return d;
  }

  latest(key: keyof HistoryPoint): string {
    const d = this.data();
    if (!d.length) return '0';
    const v = Number(d[d.length - 1][key]);
    return this.integer() ? String(v) : v.toFixed(2);
  }

  spanLabel = computed(() => {
    const d = this.data();
    return d.length < 2 ? '' : `last ${Math.round((d[d.length - 1].t - d[0].t) / 1000)}s`;
  });

  move(e: MouseEvent): void {
    const rect = (e.currentTarget as SVGElement).getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    const len = this.data().length;
    if (!len) return;
    // find the nearest point by its svg x
    const svgX = frac * this.W;
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < len; i++) {
      const dist = Math.abs(this.x(i, len) - svgX);
      if (dist < bestDist) { bestDist = dist; best = i; }
    }
    this.hover.set(best);
  }

  hoverX = computed(() => {
    const h = this.hover(), len = this.data().length;
    return h === null || h >= len ? null : this.x(h, len);
  });

  tip = computed(() => {
    const h = this.hover(), d = this.data();
    if (h === null || h >= d.length) return null;
    const p = d[h];
    return {
      leftPct: (this.x(h, d.length) / this.W) * 100,
      time: new Date(p.t).toLocaleTimeString(),
      rows: this.series().map(s => ({
        label: s.label, color: s.color, value: this.integer() ? String(Number(p[s.key])) : Number(p[s.key]).toFixed(2),
      })),
    };
  });
}
