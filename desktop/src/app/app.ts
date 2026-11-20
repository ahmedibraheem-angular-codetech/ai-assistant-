import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { ThemeService } from './core/theme';

interface NavItem {
  path: string;
  label: string;
  icon: string;
  exact: boolean;
}

/** App shell: side menu + the current page. Only pages that work are listed. */
@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  readonly theme = inject(ThemeService);

  readonly nav: NavItem[] = [
    { path: '/', label: 'الرئيسية', exact: true, icon: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z' },
    { path: '/chat', label: 'المحادثات', exact: false, icon: 'M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z' },
  ];
}
