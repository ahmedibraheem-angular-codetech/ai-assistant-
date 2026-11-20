// Rules for the team and agent pages. Pure functions, unit-tested.

import { AgentActivity, AgentProfile, AgentTemplate, Lesson, Skill } from '../core/models';
import { Tone, ago, fmt } from '../dashboard/dashboard-view';

/** "3 / 50" against a daily limit, or just the count when there is none. */
export function usage(used: number, limit: number | null | undefined): string {
  return limit ? `${fmt(used)} / ${fmt(limit)}` : fmt(used);
}

export function usageRatio(used: number, limit: number | null | undefined): number {
  return limit ? Math.min(1, used / limit) : 0;
}

export function activityLine(a: AgentActivity | undefined): string {
  if (!a) return 'لا نشاط بعد';
  const parts = [`${fmt(a.chat_turns)} رد`, `${fmt(a.tool_actions)} أداة`];
  if (a.last_active_at) parts.push(`آخر نشاط ${ago(a.last_active_at)}`);
  return parts.join(' · ');
}

const SOURCE: Record<string, string> = {
  self: 'تعلّمه بنفسه',
  user: 'منك',
  feedback: 'من تقييمك',
  study: 'من وقت المذاكرة',
};

export function lessonSource(lesson: Lesson): string {
  return SOURCE[lesson.source] ?? lesson.source;
}

const SKILL: Record<Skill['status'], { label: string; tone: Tone }> = {
  candidate: { label: 'مرشحة', tone: 'warn' },
  pending: { label: 'بانتظارك', tone: 'warn' },
  active: { label: 'نشطة', tone: 'ok' },
  rejected: { label: 'مرفوضة', tone: 'muted' },
  retired: { label: 'متقاعدة', tone: 'muted' },
};

export function skillStatus(skill: Skill): { label: string; tone: Tone } {
  return SKILL[skill.status] ?? { label: skill.status, tone: 'muted' };
}

export function evalDecision(decision: string): { label: string; tone: Tone } {
  switch (decision) {
    case 'promoted':
    case 'accepted':
      return { label: 'رُقّيت', tone: 'ok' };
    case 'rejected':
      return { label: 'رُفضت', tone: 'danger' };
    default:
      return { label: decision, tone: 'muted' };
  }
}

export function score(value: number | null): string {
  return value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;
}

/** How many things in the profile wait for the user. */
export function waitingCount(p: AgentProfile): number {
  return p.lessons_pending.length + p.skills_waiting.length;
}

/** One topic per line or comma; trimmed, unique, at most `max`. */
export function parseTopics(text: string, max = 10): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(/[\n,،]/)) {
    const topic = raw.replace(/\s+/g, ' ').trim();
    if (topic && !seen.has(topic)) seen.add(topic);
  }
  return [...seen].slice(0, max);
}

export function intervalLabel(hours: number): string {
  if (hours % 24 === 0) return hours === 24 ? 'كل يوم' : `كل ${hours / 24} أيام`;
  return `كل ${hours} ساعة`;
}

export function budgetLabel(t: AgentTemplate): string {
  const parts: string[] = [];
  if (t.daily_tool_calls) parts.push(`${fmt(t.daily_tool_calls)} أداة يومياً`);
  if (t.daily_chat_turns) parts.push(`${fmt(t.daily_chat_turns)} رد يومياً`);
  return parts.length ? parts.join(' · ') : 'بلا حدود يومية';
}

/** Time until a moment: "بعد 3 س", or "الآن" when it is due. */
export function until(iso: string, now: Date = new Date()): string {
  const minutes = Math.floor((new Date(iso).getTime() - now.getTime()) / 60000);
  if (minutes <= 0) return 'الآن';
  if (minutes < 60) return `بعد ${minutes} د`;
  if (minutes < 1440) return `بعد ${Math.floor(minutes / 60)} س`;
  return `بعد ${Math.floor(minutes / 1440)} يوم`;
}
