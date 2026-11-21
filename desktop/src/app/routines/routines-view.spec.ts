import { MORNING_PRESET, daysText, statusText, statusTone, toggleDay, validateRoutine } from './routines-view';

describe('routines-view', () => {
  it('describes days', () => {
    expect(daysText([])).toBe('كل يوم');
    expect(daysText([0, 1, 2, 3, 4, 5, 6])).toBe('كل يوم');
    expect(daysText([0, 5])).toBe('السبت، الاثنين');
  });

  it('toggles days and keeps them sorted', () => {
    expect(toggleDay([2], 0)).toEqual([0, 2]);
    expect(toggleDay([0, 2], 0)).toEqual([2]);
  });

  it('shows status text and tone', () => {
    expect(statusText({ last_status: null, last_error: null })).toBe('لم يعمل بعد');
    expect(statusText({ last_status: 'refused', last_error: 'paused' })).toContain('paused');
    expect(statusTone('ok')).toBe('ok');
    expect(statusTone('refused')).toBe('warn');
    expect(statusTone('error')).toBe('danger');
    expect(statusTone(null)).toBe('muted');
  });

  it('validates the form', () => {
    expect(validateRoutine('a', 'b', '08:00').ok).toBe(true);
    expect(validateRoutine(' ', 'b', '08:00').ok).toBe(false);
    expect(validateRoutine('a', '', '08:00').ok).toBe(false);
    expect(validateRoutine('a', 'b', '8:00').ok).toBe(false);
    expect(validateRoutine('a', 'b'.repeat(4001), '08:00').ok).toBe(false);
    expect(validateRoutine(MORNING_PRESET.name, MORNING_PRESET.prompt, MORNING_PRESET.time).ok).toBe(true);
  });
});
