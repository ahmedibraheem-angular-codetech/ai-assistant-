import { Routine } from '../core/models';

/** Display order of the day chips: week starts Saturday. Values follow the API (Monday = 0). */
export const DAY_CHIPS: { value: number; label: string }[] = [
  { value: 5, label: 'السبت' }, { value: 6, label: 'الأحد' }, { value: 0, label: 'الاثنين' },
  { value: 1, label: 'الثلاثاء' }, { value: 2, label: 'الأربعاء' }, { value: 3, label: 'الخميس' },
  { value: 4, label: 'الجمعة' },
];

export const MORNING_PRESET = {
  name: 'ملخص الصبح',
  time: '08:00',
  prompt:
    'اكتب لي ملخصاً قصيراً لصباح اليوم: ما الموافقات المعلقة، وما مهام المشاريع المفتوحة، وعلى ماذا أركز اليوم. اجعله في نقاط قصيرة ومرتبة.',
};

export function daysText(days: number[]): string {
  if (!days.length || days.length === 7) return 'كل يوم';
  return DAY_CHIPS.filter((d) => days.includes(d.value)).map((d) => d.label).join('، ');
}

export function toggleDay(days: number[], day: number): number[] {
  return days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort((a, b) => a - b);
}

export function statusText(r: Pick<Routine, 'last_status' | 'last_error'>): string {
  switch (r.last_status) {
    case 'ok': return 'نجح';
    case 'refused': return 'مرفوض: ' + (r.last_error ?? '');
    case 'error': return 'فشل: ' + (r.last_error ?? '');
    default: return 'لم يعمل بعد';
  }
}

export function statusTone(status: Routine['last_status']): 'ok' | 'warn' | 'danger' | 'muted' {
  return status === 'ok' ? 'ok' : status === 'refused' ? 'warn' : status === 'error' ? 'danger' : 'muted';
}

export type RoutineCheck = { ok: true } | { ok: false; error: string };

export function validateRoutine(name: string, prompt: string, time: string): RoutineCheck {
  if (!name.trim()) return { ok: false, error: 'اكتب اسم المهمة.' };
  if (name.trim().length > 80) return { ok: false, error: 'الاسم أطول من 80 حرفاً.' };
  if (!prompt.trim()) return { ok: false, error: 'اكتب نص الطلب.' };
  if (prompt.trim().length > 4000) return { ok: false, error: 'النص أطول من 4000 حرف.' };
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) return { ok: false, error: 'اختر وقتاً صحيحاً.' };
  return { ok: true };
}
