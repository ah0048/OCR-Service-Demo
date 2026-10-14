import { Component, input } from '@angular/core';

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
  side = input<'front' | 'back'>('front');
  /** real image URL, once images are available */
  src = input<string | null>(null);
  /** accent colour (e.g. the outcome colour) for the top stripe */
  accent = input('#e1a82d');
  alt = input('ID image');
}
