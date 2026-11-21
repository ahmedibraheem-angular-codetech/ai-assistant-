import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { ApprovalsStore } from '../core/approvals-store';
import { VoiceService } from '../core/voice';
import { FakeVoice } from '../core/voice-fake';
import { QuickPage } from './quick-page';

function setup(voice = new FakeVoice()) {
  const invoke: Invoke = async <T>(command: string, args?: Record<string, unknown>) => {
    if (command !== 'api_request') return undefined as T;
    if (args?.['path'] === '/api/v1/chat') return { status: 200, body: { model: 'm', answer: 'تمام', conversation_id: null } } as T;
    return { status: 200, body: { approvals: [], count: 0 } } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }, { provide: VoiceService, useValue: voice }] });
  const fixture = TestBed.createComponent(QuickPage);
  fixture.detectChanges();
  return { fixture, page: fixture.componentInstance, voice, el: fixture.nativeElement as HTMLElement };
}

describe('QuickPage voice', () => {
  afterEach(() => TestBed.inject(ApprovalsStore).stop());

  it('mic records, then asks with the transcript', async () => {
    const { fixture, page, voice, el } = setup();
    el.querySelector<HTMLButtonElement>('.mic')?.click();
    expect(voice.callback).not.toBeNull();
    voice.callback?.('سؤال صوتي');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('.q')?.textContent).toContain('سؤال صوتي');
    expect(page.exchanges()[0].answer).toBe('تمام');
  });

  it('speaks the answer only when autoread is on', async () => {
    const { page, voice } = setup();
    await page.ask('أ');
    expect(voice.spoken).toEqual([]);
    voice.autoread.set(true);
    await page.ask('ب');
    expect(voice.spoken).toEqual(['تمام']);
  });

  it('hides the mic when speech input is not ready', () => {
    const voice = new FakeVoice();
    voice.status.update((s) => ({ ...s, stt_ready: false }));
    expect(setup(voice).el.querySelector('.mic')).toBeNull();
  });
});
