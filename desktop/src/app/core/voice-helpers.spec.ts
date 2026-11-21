import { base64ToBlob, baseMime, blobToBase64, isAcceptedMime, parseStatus, prepareSpeech, stripForSpeech } from './voice-helpers';

describe('voice helpers', () => {
  it('checks mime types, ignoring parameters and case', () => {
    expect(isAcceptedMime('audio/mp4')).toBe(true);
    expect(isAcceptedMime('Audio/MP4;codecs=mp4a.40.2')).toBe(true);
    expect(isAcceptedMime('audio/webm;codecs=opus')).toBe(false);
    expect(isAcceptedMime('')).toBe(false);
    expect(baseMime('audio/wav; x=1')).toBe('audio/wav');
  });

  it('strips markdown symbols', () => {
    const text = '# عنوان\n- **نقطة** مع `كود` و[رابط](http://x.y)\n```js\nalert(1)\n```\n1. ثاني';
    const out = stripForSpeech(text);
    expect(out).toBe('عنوان نقطة مع كود ورابط ثاني');
    expect(out).not.toMatch(/[#*`>_|~]/);
  });

  it('cuts to the limit', () => {
    expect(prepareSpeech('**' + 'ا'.repeat(50), 10)).toHaveLength(10);
    expect(prepareSpeech('hi', 0)).toBe('hi');
    expect(prepareSpeech('   ', 10)).toBe('');
  });

  it('encodes and decodes base64 through a blob', async () => {
    const blob = new Blob([new Uint8Array([1, 2, 3, 250])], { type: 'audio/mp4' });
    const b64 = await blobToBase64(blob);
    expect(b64).toBe('AQID+g==');
    const back = base64ToBlob(b64, 'audio/mp4');
    expect(back.size).toBe(4);
    expect(back.type).toBe('audio/mp4');
  });

  it('parses status defensively', () => {
    expect(parseStatus(null).stt_ready).toBe(false);
    const s = parseStatus({ stt_ready: true, voices: ['a', 3, 'b'], model: 'm', max_audio_bytes: 5 });
    expect(s.voices).toEqual(['a', 'b']);
    expect(s.max_audio_bytes).toBe(5);
  });
});
