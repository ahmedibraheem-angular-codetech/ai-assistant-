import { Injectable, inject, signal } from '@angular/core';

import { AssistantApi } from './api';
import {
  MAX_RECORD_SECONDS, NO_VOICE_STATUS, VoiceStatus, baseMime, base64ToBlob, blobToBase64, isAcceptedMime,
  parseStatus, prepareSpeech, readAutoread, readVoiceName, writeAutoread, writeVoiceName,
} from './voice-helpers';

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** Microphone input (record -> transcribe) and read-aloud, all through the local backend. */
@Injectable({ providedIn: 'root' })
export class VoiceService {
  private readonly api = inject(AssistantApi);

  readonly status = signal<VoiceStatus>(NO_VOICE_STATUS);
  readonly recording = signal(false);
  readonly busy = signal(false);
  readonly speaking = signal(false);
  readonly error = signal('');
  readonly elapsed = signal(0);
  readonly autoread = signal(readAutoread());
  readonly voiceName = signal(readVoiceName());

  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  private size = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private onText: ((text: string) => void) | null = null;
  private audio: HTMLAudioElement | null = null;
  private audioUrl: string | null = null;

  async loadStatus(): Promise<void> {
    try {
      this.status.set(parseStatus(await this.api.request<unknown>('GET', '/api/v1/voice/status')));
    } catch {
      this.status.set(NO_VOICE_STATUS);
    }
  }

  setAutoread(on: boolean): void {
    this.autoread.set(on);
    writeAutoread(on);
  }

  setVoice(name: string): void {
    this.voiceName.set(name);
    writeVoiceName(name);
  }

  /** Start recording; `onText` receives the transcript once recording stops. */
  async start(onText: (text: string) => void): Promise<void> {
    if (this.recording() || this.busy()) return;
    this.stopSpeaking();
    this.error.set('');
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const preferred = typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported('audio/mp4');
      this.recorder = preferred ? new MediaRecorder(this.stream, { mimeType: 'audio/mp4' }) : new MediaRecorder(this.stream);
    } catch {
      this.release();
      this.error.set('تعذّر الوصول إلى الميكروفون. اسمح للمساعد بالميكروفون من إعدادات النظام.');
      return;
    }
    this.onText = onText;
    this.chunks = [];
    this.size = 0;
    const max = this.status().max_audio_bytes;
    this.recorder.ondataavailable = (event: BlobEvent) => {
      if (!event.data?.size) return;
      this.chunks.push(event.data);
      this.size += event.data.size;
      if (this.size > max) this.stop();
    };
    this.recorder.onstop = () => void this.finish();
    this.recorder.start(1000);
    this.recording.set(true);
    this.elapsed.set(0);
    this.timer = setInterval(() => {
      this.elapsed.update((n) => n + 1);
      if (this.elapsed() >= MAX_RECORD_SECONDS) this.stop();
    }, 1000);
  }

  stop(): void {
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
  }

  private release(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  private async finish(): Promise<void> {
    const type = this.recorder?.mimeType || this.chunks[0]?.type || '';
    const callback = this.onText;
    this.release();
    this.recording.set(false);
    this.recorder = null;
    const blob = new Blob(this.chunks, { type });
    this.chunks = [];
    if (!blob.size) return;
    if (!isAcceptedMime(blob.type)) {
      this.error.set('صيغة التسجيل غير مدعومة في هذا الجهاز، لذلك لم يُرسل الصوت.');
      return;
    }
    if (blob.size > this.status().max_audio_bytes) {
      this.error.set('التسجيل أكبر من الحد المسموح. سجّل مقطعاً أقصر.');
      return;
    }
    this.busy.set(true);
    try {
      const audio_base64 = await blobToBase64(blob);
      const reply = await this.api.request<{ text?: string }>('POST', '/api/v1/voice/transcribe', {
        audio_base64, mime: baseMime(blob.type),
      });
      const text = (reply?.text ?? '').trim();
      if (text) callback?.(text);
      else this.error.set('لم أسمع كلاماً واضحاً. حاول مرة أخرى.');
    } catch (error) {
      this.error.set(msg(error));
    } finally {
      this.busy.set(false);
    }
  }

  async speak(text: string): Promise<void> {
    const clean = prepareSpeech(text, this.status().max_tts_chars);
    if (!clean) return;
    this.stopSpeaking();
    this.error.set('');
    try {
      const body: { text: string; voice?: string } = { text: clean };
      if (this.voiceName()) body.voice = this.voiceName();
      const reply = await this.api.request<{ audio_base64: string; mime: string }>('POST', '/api/v1/voice/speak', body);
      const url = URL.createObjectURL(base64ToBlob(reply.audio_base64, reply.mime || 'audio/mp4'));
      const audio = new Audio(url);
      const done = () => {
        URL.revokeObjectURL(url);
        if (this.audio === audio) {
          this.audio = null;
          this.audioUrl = null;
          this.speaking.set(false);
        }
      };
      audio.onended = done;
      audio.onerror = done;
      this.audio = audio;
      this.audioUrl = url;
      this.speaking.set(true);
      await audio.play().catch((error) => {
        done();
        throw error;
      });
    } catch (error) {
      this.error.set(msg(error));
    }
  }

  stopSpeaking(): void {
    const audio = this.audio;
    if (!audio) return;
    this.audio = null;
    audio.pause();
    if (this.audioUrl) URL.revokeObjectURL(this.audioUrl);
    this.audioUrl = null;
    this.speaking.set(false);
  }
}
