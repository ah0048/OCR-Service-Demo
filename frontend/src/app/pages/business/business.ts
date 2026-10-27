import { Component, ElementRef, effect, inject, signal, viewChild } from '@angular/core';
import { ImagePlaceholder } from '../../components/image-placeholder/image-placeholder';
import { DashboardStore } from '../../core/dashboard-store';
import { duration, formatDate, formatNationalId, humanize } from '../../core/fmt';
import { OUTCOME_LABEL, OUTCOME_CHIP, OUTCOME_COLOR, ResultItem } from '../../core/models';
import { ResultsFeed } from '../../core/results-feed';
import { ResultsStore } from '../../core/results-store';

type Side = 'front' | 'back';
interface FieldView { key: string; label: string; value: string; wide?: boolean; id?: boolean; }
const SIDE_FIELDS: Record<Side, { key: string; label: string; wide?: boolean; id?: boolean }[]> = {
  front: [
    { key: 'fullName', label: 'Full name', wide: true },
    { key: 'front.id', label: 'National ID', id: true },
    { key: 'birthDate', label: 'Date of birth (from ID)' },
    { key: 'address', label: 'Address', wide: true },
  ],
  back: [
    { key: 'back.id', label: 'National ID', wide: true, id: true },
    { key: 'expiryDate', label: 'Expiry date', wide: true },
    { key: 'gender', label: 'Gender' },
    { key: 'religion', label: 'Religion' },
    { key: 'maritalStatus', label: 'Marital status' },
    { key: 'occupation', label: 'Occupation', wide: true },
    { key: 'spouseName', label: 'Spouse name', wide: true },
  ],
};

/**
 * Business view of the live results: only what the OCR read from each ID (plus job id, confidence and OCR time),
 * shown as plain labelled data. The technical details live on the Tech view page.
 */
@Component({
  selector: 'app-business',
  imports: [ImagePlaceholder],
  templateUrl: './business.html',
  styleUrl: './business.scss',
})
export class Business {
  private dashboard = inject(DashboardStore);
  protected feed = new ResultsFeed(inject(ResultsStore), this.dashboard);

  protected color = OUTCOME_COLOR;
  protected outcomeChip = OUTCOME_CHIP;
  protected sides: { key: Side; label: string }[] = [
    { key: 'front', label: 'Front' },
    { key: 'back', label: 'Back' },
  ];
  private firstGrid = viewChild<ElementRef<HTMLElement>>('reviewGrid');
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private ratios = signal<Record<string, Partial<Record<Side, number>>>>({});

  protected imageLoaded(jobId: string, side: Side, size: { width: number; height: number }): void {
    this.ratios.update(all => ({ ...all, [jobId]: { ...all[jobId], [side]: size.width / size.height } }));
  }

  protected scanWidth(r: ResultItem): string {
    const ratios = this.ratios()[r.jobId];
    const heightPerWidth = 1 / (ratios?.front ?? 85.6 / 54) + 1 / (ratios?.back ?? 85.6 / 54);
    return 'calc((var(--review-space, 30rem) - 2.75rem) / ' + heightPerWidth + ')';
  }

  protected scanHeight(r: ResultItem, side: Side): string {
    const ratio = this.ratios()[r.jobId]?.[side] ?? 85.6 / 54;
    // Add a further 20% to the previous height while preserving widths and field layout.
    return 'calc(var(--scan-original-width, min(100cqi, var(--scan-width, 100cqi))) / ' + ratio + ' * 1.32)';
  }

  constructor() {
    effect(onCleanup => {
      const grid = this.firstGrid()?.nativeElement;
      if (!grid) return;
      const fit = () => {
        const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
        const top = grid.getBoundingClientRect().top + window.scrollY;
        const available = Math.max(14 * rem, window.innerHeight - top - 1.5 * rem);
        this.host.nativeElement.style.setProperty('--review-space', (available / rem) + 'rem');
      };
      const observer = new ResizeObserver(fit);
      for (const element of [grid.previousElementSibling, document.querySelector('app-business .page-head'), document.querySelector('.topbar'), document.querySelector('.banner')])
        if (element) observer.observe(element);
      window.addEventListener('resize', fit);
      const frame = requestAnimationFrame(fit);
      onCleanup(() => {
        observer.disconnect();
        window.removeEventListener('resize', fit);
        cancelAnimationFrame(frame);
      });
    });
  }

  protected outcomeLabel = OUTCOME_LABEL;
  protected dur = duration;
  protected confColor = (c: number) => (c >= 0.8 ? '#1f9d6b' : c >= 0.5 ? '#e39a1b' : '#dc4350');

  /** Keep each side's actual ID visible, including conflicting readings. */
  protected sideFields(r: ResultItem, side: Side): FieldView[] {
    const raw = r.ocr.fields;
    if (!raw || Object.keys(raw).length === 0) return [];
    const legacy = !('front.id' in raw) && !('back.id' in raw);
    const fields: FieldView[] = SIDE_FIELDS[side].map(field => {
      const value = raw[field.key] ?? (field.id && legacy ? raw['nationalId'] : '') ?? '';
      return { ...field, value: field.id && value ? formatNationalId(value) : field.key.endsWith('Date') && value ? formatDate(value) : value };
    });
    const known = new Set(['nationalId', ...SIDE_FIELDS.front.map(f => f.key), ...SIDE_FIELDS.back.map(f => f.key)]);
    // Preserve additional extracted fields, grouping explicit side prefixes correctly.
    for (const key of Object.keys(raw).filter(k => !known.has(k))) {
      const belongs = key.startsWith('front.') ? 'front' : 'back';
      if (belongs === side) fields.push({ key, label: humanize(key.replace(/^(front|back)\./, '')), value: raw[key], wide: true });
    }
    return fields;
  }

  protected copy(text: string, what: string): void {
    navigator.clipboard?.writeText(text);
    this.dashboard.toast('info', `${what} copied`);
  }
}
