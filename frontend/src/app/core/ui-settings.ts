import { Injectable, computed, signal } from '@angular/core';

const STEPS = [0.9, 1, 1.15, 1.3, 1.5, 1.7];
const KEY = 'egx-ocr-ui-scale';

/**
 * The user's chosen text size. The whole interface is sized in rem, so changing the root font size
 * (through the --ui-scale variable read in styles.scss) enlarges everything at once.
 */
@Injectable({ providedIn: 'root' })
export class UiSettings {
  readonly scale = signal(1);
  readonly percent = computed(() => Math.round(this.scale() * 100));
  readonly canGrow = computed(() => this.scale() < STEPS[STEPS.length - 1]);
  readonly canShrink = computed(() => this.scale() > STEPS[0]);

  constructor() {
    let saved = 1;
    try { saved = Number(localStorage.getItem(KEY)) || 1; } catch { /* storage unavailable: use the default */ }
    this.set(STEPS.includes(saved) ? saved : 1);
  }

  bigger(): void { this.set(STEPS[Math.min(STEPS.length - 1, STEPS.indexOf(this.scale()) + 1)]); }
  smaller(): void { this.set(STEPS[Math.max(0, STEPS.indexOf(this.scale()) - 1)]); }
  reset(): void { this.set(1); }

  private set(value: number): void {
    this.scale.set(value);
    document.documentElement.style.setProperty('--ui-scale', String(value));
    try { localStorage.setItem(KEY, String(value)); } catch { /* ignore */ }
  }
}
