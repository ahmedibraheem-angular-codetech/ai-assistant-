import { budgetLabel, evalDecision, intervalLabel, parseTopics, score, until, usage, usageRatio, waitingCount } from './team-view';
import { TEMPLATE, profile } from './team.testing';

describe('team rules', () => {
  it('shows usage against a limit, or just the count', () => {
    expect(usage(5, 50)).toBe('5 / 50');
    expect(usage(5, null)).toBe('5');
    expect(usageRatio(60, 50)).toBe(1);
    expect(usageRatio(5, null)).toBe(0);
  });

  it('parses study topics from lines or commas, unique, capped', () => {
    expect(parseTopics('إعلانات فيسبوك\n كتابة   المحتوى ، إعلانات فيسبوك,  ')).toEqual(['إعلانات فيسبوك', 'كتابة المحتوى']);
    expect(parseTopics(Array.from({ length: 15 }, (_, i) => `t${i}`).join('\n'))).toHaveLength(10);
    expect(parseTopics('  \n , ')).toEqual([]);
  });

  it('labels intervals, scores, decisions and budgets', () => {
    expect(intervalLabel(24)).toBe('كل يوم');
    expect(intervalLabel(72)).toBe('كل 3 أيام');
    expect(intervalLabel(6)).toBe('كل 6 ساعة');
    expect(score(0.5)).toBe('50%');
    expect(score(null)).toBe('—');
    expect(evalDecision('promoted').tone).toBe('ok');
    expect(evalDecision('rejected').label).toBe('رُفضت');
    expect(budgetLabel(TEMPLATE)).toBe('50 أداة يومياً');
    expect(budgetLabel({ ...TEMPLATE, daily_tool_calls: null })).toBe('بلا حدود يومية');
  });

  it('counts what waits for review and the time to the next run', () => {
    expect(waitingCount(profile())).toBe(2);
    const now = new Date('2026-11-21T10:00:00Z');
    expect(until('2026-11-21T13:00:00Z', now)).toBe('بعد 3 س');
    expect(until('2026-11-21T09:00:00Z', now)).toBe('الآن');
  });
});
