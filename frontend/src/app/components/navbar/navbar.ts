import { Component, HostListener, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { DashboardStore } from '../../core/dashboard-store';

@Component({
  selector: 'app-navbar',
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './navbar.html',
  styleUrl: './navbar.scss',
})
export class Navbar {
  protected store = inject(DashboardStore);
  protected open = signal(false);

  @HostListener('document:click') close() { this.open.set(false); }
}
