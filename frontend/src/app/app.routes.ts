import { Routes } from '@angular/router';
import { Batches } from './pages/batches/batches';
import { Jobs } from './pages/jobs/jobs';
import { Overview } from './pages/overview/overview';
import { Pipeline } from './pages/pipeline/pipeline';
import { Business } from './pages/business/business';
import { Results } from './pages/results/results';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'overview' },
  { path: 'overview', component: Overview, title: 'Overview · EGX ID OCR' },
  { path: 'pipeline', component: Pipeline, title: 'Queues & workers · EGX ID OCR' },
  { path: 'business', component: Business, title: 'Business view · EGX ID OCR' },
  { path: 'results', component: Results, title: 'OCR results · EGX ID OCR' },
  { path: 'jobs', component: Jobs, title: 'Jobs · EGX ID OCR' },
  { path: 'batches', component: Batches, title: 'Batches · EGX ID OCR' },
  { path: '**', redirectTo: 'overview' },
];
