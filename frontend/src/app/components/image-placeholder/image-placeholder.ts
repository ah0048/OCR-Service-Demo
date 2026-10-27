import { Component, computed, input, output, signal } from '@angular/core';

import { API_URL } from '../../core/api';

/**
 * Stand-in for one scanned side of the ID card (front or back). When real images exist, pass `src` and the picture replaces the drawing;
 * nothing else on the page needs to change.
 */
@Component({
  selector: 'app-image-placeholder',
  templateUrl: './image-placeholder.html',
  styleUrl: './image-placeholder.scss',
})
export class ImagePlaceholder {
  /** which side of the card this is */
  jobId = input<string | null>(null);
  imageReference = input<string | null>(null);
  imageUrl = computed(() => this.src() ?? (this.jobId() && /^[a-f0-9]{32}$/i.test(this.imageReference() ?? '')
    ? API_URL + '/api/jobs/' + this.jobId() + '/images/' + this.side() : null));
  side = input<'front' | 'back'>('front');
  /** real image URL, once images are available */
  src = input<string | null>(null);
  fit = input<'cover' | 'contain' | 'fill'>('cover');
  frameHeight = input<string | null>(null);
  naturalAspect = input(false);
  protected naturalRatio = signal(85.6 / 54);
  dimensions = output<{ width: number; height: number }>();

  protected imageLoaded(event: Event): void {
    const image = event.target as HTMLImageElement;
    if (image.naturalWidth && image.naturalHeight) {
      this.naturalRatio.set(image.naturalWidth / image.naturalHeight);
      this.dimensions.emit({ width: image.naturalWidth, height: image.naturalHeight });
    }
  }
  showSideLabel = input(true);
  /** accent colour (e.g. the outcome colour) for the top stripe */
  accent = input('#e1a82d');
  alt = input('ID image');
}
