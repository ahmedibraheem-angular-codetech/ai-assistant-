import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { App } from './app';
import { routes } from './app.routes';
import { TAURI_INVOKE } from './core/api';
import { ThemeService } from './core/theme';

describe('App', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideRouter(routes), { provide: TAURI_INVOKE, useValue: () => Promise.reject('offline') }],
    });
  });

  it('opens the dashboard by default, with the side menu', async () => {
    const fixture = TestBed.createComponent(App);
    await TestBed.inject(Router).navigateByUrl('/');
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-dashboard-page')).toBeTruthy();
    expect(el.querySelector('nav a.active')?.textContent).toContain('الرئيسية');
  });

  it('unknown paths go home; /chat opens the chat', async () => {
    const fixture = TestBed.createComponent(App);
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/nowhere');
    expect(router.url).toBe('/');
    await router.navigateByUrl('/chat');
    await fixture.whenStable();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('app-chat-page')).toBeTruthy();
  });

  it('the theme button switches dark and light', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    const theme = TestBed.inject(ThemeService);
    const start = theme.theme();
    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('.theme')?.click();
    expect(theme.theme()).not.toBe(start);
    expect(document.documentElement.dataset['theme']).toBe(theme.theme());
    theme.set(start);
  });
});
