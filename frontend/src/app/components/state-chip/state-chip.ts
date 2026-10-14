import { Component, computed, input } from '@angular/core';
import { JobOutcome, JobStatus, OUTCOME_CHIP, OUTCOME_LABEL } from '../../core/models';

@Component({
  selector: 'app-state-chip',
  templateUrl: './state-chip.html',
})
export class StateChip {
  status = input.required<JobStatus>();
  outcome = input<JobOutcome | null>(null);
  chipClass = computed(() => OUTCOME_CHIP[this.outcome() ?? ''] ?? 'slate');
  label = computed(() => OUTCOME_LABEL[this.outcome() ?? ''] ?? this.outcome());
}
