import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { JobDetail, JobFilter, JobPage } from './models';

/** Base URL of OcrDemo.Api. Change it here if the API runs elsewhere. */
export const API_URL = 'http://localhost:5125';

export interface SubmitResult { jobId: string; statusUrl: string; }
export interface BatchResult { batchId: string; count: number; firstJobId: string; }

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  listJobs(f: JobFilter): Observable<JobPage> {
    let p = new HttpParams().set('page', f.page).set('pageSize', f.pageSize);
    if (f.status) p = p.set('status', f.status);
    if (f.outcome) p = p.set('outcome', f.outcome);
    if (f.search.trim()) p = p.set('search', f.search.trim());
    if (f.batchId) p = p.set('batchId', f.batchId);
    return this.http.get<JobPage>(`${API_URL}/api/jobs`, { params: p });
  }

  getJob(id: string): Observable<JobDetail> {
    return this.http.get<JobDetail>(`${API_URL}/api/jobs/${id}`);
  }

  uploadJob(clientReference: string, front: File, back: File): Observable<SubmitResult> {
    const form = new FormData();
    form.append('clientReference', clientReference);
    form.append('front', front);
    form.append('back', back);
    return this.http.post<SubmitResult>(API_URL + '/api/jobs/upload', form);
  }

  uploadBatch(batchName: string, pairs: { reference: string; front: File; back: File }[]): Observable<BatchResult> {
    const form = new FormData();
    form.append('batchName', batchName);
    for (const pair of pairs) {
      form.append('clientReference', pair.reference);
      form.append('front', pair.front);
      form.append('back', pair.back);
    }
    return this.http.post<BatchResult>(API_URL + '/api/jobs/batch/upload', form);
  }
}
