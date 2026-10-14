import { Component, HostListener, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { DashboardStore } from '../../core/dashboard-store';
import { UiSettings } from '../../core/ui-settings';

@Component({
  selector: 'app-navbar',
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './navbar.html',
  styleUrl: './navbar.scss',
})
export class Navbar {
  protected store = inject(DashboardStore);
  protected ui = inject(UiSettings);
  protected open = signal(false);

  @HostListener('document:click') close() { this.open.set(false); }
}
