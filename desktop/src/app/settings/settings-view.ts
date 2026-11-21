// Pure helpers for the settings page: no Angular, no network.

import { BudgetLimits, NewStandingApproval, StandingApproval } from '../core/models';

export const LIMIT_MIN = 1;
export const LIMIT_MAX = 100000;
export const DAYS_MAX = 365;

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

/** Empty text means "no limit" (null); otherwise a whole number from 1 to 100000. */
export function parseLimit(text: string): Parsed<number | null> {
  const t = text.trim();
  if (!t) return { ok: true, value: null };
  if (!/^\d+$/.test(t)) return { ok: false, error: 'اكتب رقماً صحيحاً أو اتركه فارغاً بلا حد.' };
  const n = Number(t);
  if (n < LIMIT_MIN || n > LIMIT_MAX) return { ok: false, error: `الحد بين ${LIMIT_MIN} و${LIMIT_MAX}.` };
  return { ok: true, value: n };
}

export function validateBudget(tools: string, chats: string): Parsed<BudgetLimits> {
  const t = parseLimit(tools);
  if (!t.ok) return t;
  const c = parseLimit(chats);
  if (!c.ok) return c;
  if (t.value === null && c.value === null) {
    return { ok: false, error: 'اكتب حداً واحداً على الأقل، أو استخدم «إزالة الحدود».' };
  }
  return { ok: true, value: { daily_tool_calls: t.value, daily_chat_turns: c.value } };
}

export function validateStanding(form: { agentId: string; tool: string; path: string; days: string }): Parsed<NewStandingApproval> {
  const tool = form.tool.trim();
  if (!form.agentId) return { ok: false, error: 'اختر الوكيل.' };
  if (!tool) return { ok: false, error: 'اكتب اسم أداة القراءة.' };
  const body: NewStandingApproval = { agent_id: form.agentId, tool_name: tool };
  const path = form.path.trim();
  if (path) body.path_prefix = path;
  const d = form.days.trim();
  if (d) {
    const n = /^\d+$/.test(d) ? Number(d) : NaN;
    if (!(n >= 1 && n <= DAYS_MAX)) return { ok: false, error: `المدة بين 1 و${DAYS_MAX} يوماً، أو اتركها فارغة بلا انتهاء.` };
    body.expires_in_days = n;
  }
  return { ok: true, value: body };
}

export function limitText(limit: number | null): string {
  return limit === null ? 'بلا حد' : String(limit);
}

/** "12 / 50" or "12 / بلا حد". */
export function usageText(used: number, limit: number | null): string {
  return `${used} / ${limitText(limit)}`;
}

/** 0..100 for a progress bar, or null when there is no limit. */
export function usagePercent(used: number, limit: number | null): number | null {
  if (limit === null || limit <= 0) return null;
  return Math.min(100, Math.round((used / limit) * 100));
}

export function usageTone(used: number, limit: number | null): 'muted' | 'ok' | 'warn' | 'danger' {
  const p = usagePercent(used, limit);
  if (p === null) return 'muted';
  return p >= 100 ? 'danger' : p >= 80 ? 'warn' : 'ok';
}

export function hasLimits(b: BudgetLimits): boolean {
  return b.daily_tool_calls !== null || b.daily_chat_turns !== null;
}

export function standingScope(s: StandingApproval): string {
  return s.path_prefix ? `المسار: ${s.path_prefix}` : 'كل المسارات';
}

export function standingExpiry(s: StandingApproval, now = Date.now()): string {
  if (!s.active) return 'منتهية';
  if (!s.expires_at) return 'بلا انتهاء';
  const ms = Date.parse(s.expires_at) - now;
  if (Number.isNaN(ms)) return '';
  const days = Math.ceil(ms / 86_400_000);
  return days <= 1 ? 'تنتهي خلال يوم' : `تنتهي بعد ${days} يوماً`;
}
