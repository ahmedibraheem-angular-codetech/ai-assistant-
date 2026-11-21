import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { VoiceService } from '../core/voice';
import { FakeVoice } from '../core/voice-fake';
import { SettingsPage } from './settings-page';

describe('SettingsPage voice section', () => {
  it('shows status and model, picks a voice and tests it', async () => {
    const voice = new FakeVoice();
    const invoke: Invoke = async <T>() => ({ status: 200, body: { agents: [], count: 0, paused: false, standing_approvals: [] } }) as T;
    TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }, { provide: VoiceService, useValue: voice }] });
    const fixture = TestBed.createComponent(SettingsPage);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const section = el.querySelector('#s-voice')?.parentElement as HTMLElement;
    expect(section.textContent).toContain('جاهز');
    expect(section.textContent).toContain('whisper-small');
    expect(section.querySelectorAll('option').length).toBe(3);
    fixture.componentInstance.pickVoice('Majed');
    expect(voice.voiceName()).toBe('Majed');
    fixture.componentInstance.testVoice();
    expect(voice.spoken.length).toBe(1);
  });
});
