// Rules for the approvals page. Pure functions, unit-tested.

import { Approval } from '../core/models';
import { Tone, ago } from '../dashboard/dashboard-view';

export function risk(level: string): { label: string; tone: Tone } {
  switch (level) {
    case 'high':
      return { label: 'خطورة عالية', tone: 'danger' };
    case 'medium':
      return { label: 'خطورة متوسطة', tone: 'warn' };
    case 'low':
      return { label: 'خطورة منخفضة', tone: 'ok' };
    default:
      return { label: level || 'غير محددة', tone: 'muted' };
  }
}

const STATUS: Record<string, { label: string; tone: Tone }> = {
  PENDING: { label: 'بانتظارك', tone: 'warn' },
  APPROVED: { label: 'وُفق عليها', tone: 'accent' },
  EXECUTING: { label: 'قيد التنفيذ', tone: 'accent' },
  COMPLETED: { label: 'نُفذت', tone: 'ok' },
  FAILED: { label: 'فشلت', tone: 'danger' },
  REJECTED: { label: 'رفضتها', tone: 'muted' },
  EXPIRED: { label: 'انتهت مهلتها', tone: 'muted' },
  CANCELLED: { label: 'أُلغيت', tone: 'muted' },
};

export function status(a: Approval): { label: string; tone: Tone } {
  if (a.outcome === 'unknown') return { label: 'النتيجة غير معروفة', tone: 'warn' };
  return STATUS[a.status] ?? { label: a.status, tone: 'muted' };
}

export function timeLeft(expires: string | null, now: Date = new Date()): string {
  if (!expires) return '';
  const minutes = Math.floor((new Date(expires).getTime() - now.getTime()) / 60000);
  if (minutes < 0) return 'انتهت المهلة';
  if (minutes < 60) return `تنتهي خلال ${Math.max(1, minutes)} د`;
  return `تنتهي خلال ${Math.floor(minutes / 60)} س`;
}

export function when(a: Approval, now: Date = new Date()): string {
  return ago(a.decided_at ?? a.created_at, now);
}

const OPERATION: Record<string, string> = {
  create: 'إنشاء ملف',
  update: 'تعديل ملف',
  delete: 'نقل ملف إلى الاسترجاع',
};

export function changeTitle(a: Approval): string | null {
  return a.change ? `${OPERATION[a.change.operation] ?? a.change.operation}: ${a.change.relative_path}` : null;
}

/**
 * Whether the approve button may run this request. The server checks again;
 * this only avoids offering a button that would be refused.
 */
export function approvable(a: Approval): { ok: boolean; why: string } {
  if (a.status !== 'PENDING') return { ok: false, why: 'تم البت في هذا الطلب.' };
  if (a.review.mcp.applies && !a.review.mcp_execution_available) {
    return { ok: false, why: 'عمليات MCP تُعرض للمراجعة فقط، ولا يمكن تنفيذها من هنا بعد.' };
  }
  if (!a.review.verified) return { ok: false, why: 'العملية المخزنة لا تطابق بصمتها. ارفضها.' };
  if (!a.review.reviewable) return { ok: false, why: 'لا يمكن مراجعة هذا الطلب بأمان. ارفضه.' };
  return { ok: true, why: '' };
}

export function hiddenCount(a: Approval): number {
  const hidden = a.review.hidden;
  if (Array.isArray(hidden)) return hidden.length;
  return typeof hidden === 'number' ? hidden : 0;
}

/** Short text of what the tool returned after approval, for the result line. */
export function outputText(output: unknown): string {
  if (output === null || output === undefined) return '';
  const text = typeof output === 'string' ? output : JSON.stringify(output);
  return text.length > 600 ? `${text.slice(0, 600)}…` : text;
}
