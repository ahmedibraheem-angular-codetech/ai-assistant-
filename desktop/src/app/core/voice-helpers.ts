/** Pure helpers for voice input and output (no browser APIs except Blob/FileReader/localStorage). */

export const ACCEPTED_MIMES = [
  'audio/mp4', 'audio/x-m4a', 'audio/aac', 'audio/wav', 'audio/aiff', 'audio/mpeg', 'audio/caf',
] as const;

export const MAX_RECORD_SECONDS = 60;
export const DEFAULT_MAX_AUDIO_BYTES = 10 * 1024 * 1024;
export const DEFAULT_MAX_TTS_CHARS = 2000;
export const AUTOREAD_KEY = 'assistant.voice.autoread.v1';
export const VOICE_NAME_KEY = 'assistant.voice.name.v1';

export interface VoiceStatus {
  stt_ready: boolean;
  tts_ready: boolean;
  language: string;
  model: string;
  voice: string;
  voices: string[];
  max_audio_bytes: number;
  max_tts_chars: number;
}

export const NO_VOICE_STATUS: VoiceStatus = {
  stt_ready: false, tts_ready: false, language: '', model: '', voice: '', voices: [],
  max_audio_bytes: DEFAULT_MAX_AUDIO_BYTES, max_tts_chars: DEFAULT_MAX_TTS_CHARS,
};

/** `audio/mp4;codecs=mp4a.40.2` -> `audio/mp4`. */
export function baseMime(mime: string): string {
  return (mime ?? '').split(';')[0].trim().toLowerCase();
}

export function isAcceptedMime(mime: string): boolean {
  return (ACCEPTED_MIMES as readonly string[]).includes(baseMime(mime));
}

/** Remove markdown-ish symbols so the voice does not read them out. */
export function stripForSpeech(text: string): string {
  return (text ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/gm, '')
    .replace(/[`*_#>~|]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Strip and cut to the backend limit. */
export function prepareSpeech(text: string, maxChars: number): string {
  const limit = maxChars > 0 ? maxChars : DEFAULT_MAX_TTS_CHARS;
  return stripForSpeech(text).slice(0, limit).trim();
}

/** Base64 of a blob via FileReader (no large string concatenation). */
export function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.onload = () => {
      const url = String(reader.result ?? '');
      const comma = url.indexOf(',');
      resolve(comma >= 0 ? url.slice(comma + 1) : '');
    };
    reader.readAsDataURL(blob);
  });
}

export function base64ToBlob(base64: string, mime: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

export function readAutoread(): boolean {
  try {
    return localStorage.getItem(AUTOREAD_KEY) === '1';
  } catch {
    return false;
  }
}

export function writeAutoread(on: boolean): void {
  try {
    localStorage.setItem(AUTOREAD_KEY, on ? '1' : '0');
  } catch {
    /* storage unavailable: keep the in-memory value */
  }
}

export function readVoiceName(): string {
  try {
    return localStorage.getItem(VOICE_NAME_KEY) ?? '';
  } catch {
    return '';
  }
}

export function writeVoiceName(name: string): void {
  try {
    localStorage.setItem(VOICE_NAME_KEY, name);
  } catch {
    /* ignore */
  }
}

/** Defensive parse of /voice/status (older or partial replies must not break the UI). */
export function parseStatus(raw: unknown): VoiceStatus {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Partial<VoiceStatus>;
  return {
    stt_ready: r.stt_ready === true,
    tts_ready: r.tts_ready === true,
    language: typeof r.language === 'string' ? r.language : '',
    model: typeof r.model === 'string' ? r.model : '',
    voice: typeof r.voice === 'string' ? r.voice : '',
    voices: Array.isArray(r.voices) ? r.voices.filter((v) => typeof v === 'string') : [],
    max_audio_bytes: Number(r.max_audio_bytes) > 0 ? Number(r.max_audio_bytes) : DEFAULT_MAX_AUDIO_BYTES,
    max_tts_chars: Number(r.max_tts_chars) > 0 ? Number(r.max_tts_chars) : DEFAULT_MAX_TTS_CHARS,
  };
}
