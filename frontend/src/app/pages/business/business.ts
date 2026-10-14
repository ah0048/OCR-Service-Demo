import { Component, inject } from '@angular/core';
import { ImagePlaceholder } from '../../components/image-placeholder/image-placeholder';
import { DashboardStore } from '../../core/dashboard-store';
import { duration, formatDate, formatNationalId, humanize } from '../../core/fmt';
import { OUTCOME_COLOR, ResultItem } from '../../core/models';
import { ResultsFeed } from '../../core/results-feed';
import { ResultsStore } from '../../core/results-store';

interface FieldView { key: string; label: string; value: string; }

/** The fields people care about, in reading order, with the wording the business team uses. */
const FIELD_ORDER = ['fullName', 'nationalId', 'birthDate', 'expiryDate'];
const FIELD_LABEL: Record<string, string> = {
  fullName: 'Full name',
  nationalId: 'National ID',
  birthDate: 'Date of birth',
  expiryDate: 'Expiry date',
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
  protected dur = duration;
  protected confColor = (c: number) => (c >= 0.8 ? '#1f9d6b' : c >= 0.5 ? '#e39a1b' : '#dc4350');

  /** the extracted fields ready to display, or null when nothing could be read from the ID */
  protected fields(r: ResultItem): FieldView[] | null {
    const raw = r.ocr.fields;
    if (!raw || Object.keys(raw).length === 0) return null;
    const keys = [...FIELD_ORDER.filter(k => k in raw), ...Object.keys(raw).filter(k => !FIELD_ORDER.includes(k))];
    return keys.map(key => ({
      key,
      label: FIELD_LABEL[key] ?? humanize(key),
      value: key === 'nationalId' ? formatNationalId(raw[key]) : key.endsWith('Date') ? formatDate(raw[key]) : raw[key],
    }));
  }

  protected copy(text: string, what: string): void {
    navigator.clipboard?.writeText(text);
    this.dashboard.toast('info', `${what} copied`);
  }
}
