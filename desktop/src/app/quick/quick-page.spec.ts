import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { ApprovalsStore } from '../core/approvals-store';
import { QuickPage } from './quick-page';

function setup(chat: () => { status: number; body: unknown } = () => ({ status: 200, body: { model: 'm', answer: 'تمام', conversation_id: '8f7eff63-805b-4d35-83bf-2f879ccc2198' } })) {
  const calls: { command: string; args?: Record<string, unknown> }[] = [];
  const invoke: Invoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push({ command, args });
    if (command !== 'api_request') return undefined as T;
    if (args?.['path'] === '/api/v1/chat') return chat() as T;
    return { status: 200, body: { approvals: [], count: 0 } } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(QuickPage);
  fixture.detectChanges();
  return { fixture, page: fixture.componentInstance, calls, el: fixture.nativeElement as HTMLElement };
}

describe('QuickPage', () => {
  afterEach(() => TestBed.inject(ApprovalsStore).stop());

  it('asks, shows the answer, and keeps the conversation for the next question', async () => {
    const { fixture, page, calls, el } = setup();
    page.draft.set('ازيك');
    await page.ask();
    fixture.detectChanges();
    expect(el.querySelector('.a')?.textContent).toContain('تمام');
    await page.ask('وبعدين؟');
    const second = calls.filter((c) => c.args?.['path'] === '/api/v1/chat')[1];
    expect(second.args?.['body']).toEqual({ message: 'وبعدين؟', conversation_id: '8f7eff63-805b-4d35-83bf-2f879ccc2198' });
  });

  it('a suggestion sends its prompt', async () => {
    const { fixture, page, calls, el } = setup();
    el.querySelector<HTMLButtonElement>('.chips button')?.click();
    await fixture.whenStable();
    const sent = calls.find((c) => c.args?.['path'] === '/api/v1/chat');
    expect(String((sent?.args?.['body'] as { message: string }).message)).toContain('لخص');
    expect(page.exchanges()).toHaveLength(1);
  });

  it('shows a failure in place', async () => {
    const { fixture, page, el } = setup(() => ({ status: 502, body: { detail: 'connection refused' } }));
    await page.ask('hi');
    fixture.detectChanges();
    expect(el.querySelector('.a.failed')?.textContent).toContain('Ollama');
  });

  it('⌘ Enter continues in the main window, Escape hides, shortcuts open pages', async () => {
    const { page, calls } = setup();
    await page.ask('hi');
    page.onKeydown(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true }));
    await Promise.resolve();
    expect(calls).toContainEqual({ command: 'open_main', args: { path: '/chat/8f7eff63-805b-4d35-83bf-2f879ccc2198' } });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(calls.some((c) => c.command === 'hide_quick')).toBe(true);
    await page.open('/approvals');
    expect(calls).toContainEqual({ command: 'open_main', args: { path: '/approvals' } });
  });

  it('keeps only the last three exchanges', async () => {
    const { page } = setup();
    for (const q of ['1', '2', '3', '4']) await page.ask(q);
    expect(page.exchanges().map((x) => x.question)).toEqual(['2', '3', '4']);
  });
});
