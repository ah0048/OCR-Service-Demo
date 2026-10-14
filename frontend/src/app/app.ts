import { Component, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { DashboardStore } from './core/dashboard-store';
import { ResultsStore } from './core/results-store';
import { JobDrawer } from './components/job-drawer/job-drawer';
import { Navbar } from './components/navbar/navbar';
import { SubmitDialog } from './components/submit-dialog/submit-dialog';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, Navbar, JobDrawer, SubmitDialog],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  protected store = inject(DashboardStore);
  constructor() {
    this.store.connect();
    inject(ResultsStore).connect(); // keep receiving results while another page is open
  }
}
