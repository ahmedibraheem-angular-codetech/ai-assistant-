import { Injectable, signal } from '@angular/core';

export type Theme = 'dark' | 'light';
const KEY = 'assistant.theme';

/** Dark or light. Sets `data-theme` on <html>; styles.css holds the colours. */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly theme = signal<Theme>(this.stored());

  constructor() {
    this.apply(this.theme());
  }

  toggle(): void {
    this.set(this.theme() === 'dark' ? 'light' : 'dark');
  }

  set(theme: Theme): void {
    this.theme.set(theme);
    this.apply(theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      // Storage can be unavailable; the theme still applies for this session.
    }
  }

  /** Current value of a CSS colour token, for canvas charts that cannot read CSS. */
  color(name: string, fallback: string): string {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  private stored(): Theme {
    try {
      return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  }

  private apply(theme: Theme): void {
    document.documentElement.dataset['theme'] = theme;
  }
}
