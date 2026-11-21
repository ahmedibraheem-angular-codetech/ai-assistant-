import {
  hasLimits, limitText, parseLimit, standingExpiry, standingScope, usagePercent, usageText, usageTone,
  validateBudget, validateStanding,
} from './settings-view';
import { StandingApproval } from '../core/models';

describe('settings-view', () => {
  it('parseLimit: empty is no limit, numbers are range checked', () => {
    expect(parseLimit('  ')).toEqual({ ok: true, value: null });
    expect(parseLimit(' 50 ')).toEqual({ ok: true, value: 50 });
    expect(parseLimit('100000')).toEqual({ ok: true, value: 100000 });
    for (const bad of ['0', '100001', '-3', '1.5', 'abc', '1e3']) expect(parseLimit(bad).ok).toBe(false);
  });

  it('validateBudget needs at least one limit and passes nulls through', () => {
    expect(validateBudget('', '').ok).toBe(false);
    expect(validateBudget('x', '5').ok).toBe(false);
    expect(validateBudget('5', 'x').ok).toBe(false);
    expect(validateBudget('5', '')).toEqual({ ok: true, value: { daily_tool_calls: 5, daily_chat_turns: null } });
  });

  it('validateStanding builds the body and rejects bad input', () => {
    const base = { agentId: 'a1', tool: ' read_file ', path: '', days: '' };
    expect(validateStanding(base)).toEqual({ ok: true, value: { agent_id: 'a1', tool_name: 'read_file' } });
    expect(validateStanding({ ...base, path: ' docs/ ', days: '30' })).toEqual({
      ok: true, value: { agent_id: 'a1', tool_name: 'read_file', path_prefix: 'docs/', expires_in_days: 30 },
    });
    expect(validateStanding({ ...base, agentId: '' }).ok).toBe(false);
    expect(validateStanding({ ...base, tool: ' ' }).ok).toBe(false);
    for (const days of ['0', '366', 'x', '2.5']) expect(validateStanding({ ...base, days }).ok).toBe(false);
  });

  it('usage helpers', () => {
    expect(limitText(null)).toBe('بلا حد');
    expect(usageText(3, 10)).toBe('3 / 10');
    expect(usageText(3, null)).toBe('3 / بلا حد');
    expect(usagePercent(3, null)).toBeNull();
    expect(usagePercent(5, 10)).toBe(50);
    expect(usagePercent(20, 10)).toBe(100);
    expect(usageTone(1, null)).toBe('muted');
    expect(usageTone(5, 10)).toBe('ok');
    expect(usageTone(8, 10)).toBe('warn');
    expect(usageTone(10, 10)).toBe('danger');
    expect(hasLimits({ daily_tool_calls: null, daily_chat_turns: null })).toBe(false);
    expect(hasLimits({ daily_tool_calls: null, daily_chat_turns: 1 })).toBe(true);
  });

  it('standing labels', () => {
    const s: StandingApproval = {
      id: 's', agent_id: 'a', tool_name: 't', workspace_id: null, path_prefix: null,
      created_at: '2026-11-21T00:00:00Z', expires_at: null, active: true,
    };
    const now = Date.parse('2026-11-21T00:00:00Z');
    expect(standingScope(s)).toBe('كل المسارات');
    expect(standingScope({ ...s, path_prefix: 'docs/' })).toContain('docs/');
    expect(standingExpiry(s, now)).toBe('بلا انتهاء');
    expect(standingExpiry({ ...s, expires_at: '2026-11-24T00:00:00Z' }, now)).toContain('3');
    expect(standingExpiry({ ...s, expires_at: '2026-11-21T05:00:00Z' }, now)).toBe('تنتهي خلال يوم');
    expect(standingExpiry({ ...s, active: false }, now)).toBe('منتهية');
  });
});
