import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { App } from './app';
import { routes } from './app.routes';
import { TAURI_INVOKE } from './core/api';

describe('App', () => {
  it('opens the chat page by default', async () => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideRouter(routes), { provide: TAURI_INVOKE, useValue: () => Promise.reject('offline') }],
    });
    const fixture = TestBed.createComponent(App);
    await TestBed.inject(Router).navigateByUrl('/');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(TestBed.inject(Router).url).toBe('/chat');
    expect((fixture.nativeElement as HTMLElement).querySelector('app-chat-page')).toBeTruthy();
  });
});
