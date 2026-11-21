import { signal } from '@angular/core';

import { NO_VOICE_STATUS, VoiceStatus } from './voice-helpers';

/** Test double for VoiceService. */
export class FakeVoice {
  status = signal<VoiceStatus>({ ...NO_VOICE_STATUS, stt_ready: true, tts_ready: true, model: 'whisper-small', voices: ['Laila', 'Majed'] });
  recording = signal(false);
  busy = signal(false);
  speaking = signal(false);
  error = signal('');
  elapsed = signal(0);
  autoread = signal(false);
  voiceName = signal('');
  spoken: string[] = [];
  callback: ((text: string) => void) | null = null;
  loadStatus = async () => undefined;
  setAutoread = (on: boolean) => this.autoread.set(on);
  setVoice = (name: string) => this.voiceName.set(name);
  start = async (cb: (text: string) => void) => { this.callback = cb; this.recording.set(true); };
  stop = () => this.recording.set(false);
  speak = async (text: string) => { this.spoken.push(text); };
  stopSpeaking = () => this.speaking.set(false);
}
