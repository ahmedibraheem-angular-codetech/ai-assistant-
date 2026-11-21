import { Injectable, computed, signal } from '@angular/core';

/** Every widget the home page can show. Add one here and in the dashboard template. */
export const WIDGETS = [
  { id: 'insights', name: 'تنبيهات', hint: 'أشياء تحتاجك الآن', span: 12 },
  { id: 'kpis', name: 'المؤشرات', hint: 'كروت الأرقام بالحلقات', span: 12 },
  { id: 'map', name: 'خريطة المساعد', hint: 'الوكلاء والأنظمة حول المساعد', span: 8 },
  { id: 'queue', name: 'آخر الإجراءات', hint: 'طلبات الموافقة وحالتها', span: 4 },
  { id: 'trend', name: 'النشاط عبر الوقت', hint: 'منحنى الردود والأدوات', span: 4 },
  { id: 'tools', name: 'أكثر الأدوات استخداماً', hint: 'الأدوات ومتوسط مدتها', span: 4 },
  { id: 'learning', name: 'التعلم والذاكرة', hint: 'دروس ومهارات وذكريات', span: 4 },
] as const;

export type WidgetId = (typeof WIDGETS)[number]['id'];
export type Density = 'comfy' | 'compact';

export interface Layout {
  order: WidgetId[];
  hidden: WidgetId[];
  density: Density;
  motion: boolean;
}

const KEY = 'assistant.home-layout.v1';
const IDS = WIDGETS.map((w) => w.id) as WidgetId[];

export function defaultLayout(): Layout {
  return { order: [...IDS], hidden: [], density: 'comfy', motion: true };
}

/**
 * Reads a stored layout defensively: unknown widgets are dropped, widgets
 * added in a later version are appended (visible), bad values fall back.
 */
export function normalize(raw: unknown): Layout {
  const base = defaultLayout();
  if (!raw || typeof raw !== 'object') return base;
  const value = raw as Partial<Record<keyof Layout, unknown>>;
  const known = (list: unknown) => (Array.isArray(list) ? list.filter((id): id is WidgetId => IDS.includes(id as WidgetId)) : []);
  const order = [...new Set(known(value.order))];
  for (const id of IDS) if (!order.includes(id)) order.push(id);
  return {
    order,
    hidden: [...new Set(known(value.hidden))],
    density: value.density === 'compact' ? 'compact' : 'comfy',
    motion: value.motion !== false,
  };
}

/** The home page layout, kept in this computer's app storage. */
@Injectable({ providedIn: 'root' })
export class LayoutService {
  readonly layout = signal<Layout>(this.load());

  readonly visible = computed(() => {
    const { order, hidden } = this.layout();
    return order.filter((id) => !hidden.includes(id)).map((id) => WIDGETS.find((w) => w.id === id)!);
  });

  isVisible(id: WidgetId): boolean {
    return !this.layout().hidden.includes(id);
  }

  toggle(id: WidgetId): void {
    const { hidden } = this.layout();
    this.update({ hidden: hidden.includes(id) ? hidden.filter((h) => h !== id) : [...hidden, id] });
  }

  move(id: WidgetId, step: -1 | 1): void {
    const order = [...this.layout().order];
    const from = order.indexOf(id);
    const to = from + step;
    if (from < 0 || to < 0 || to >= order.length) return;
    [order[from], order[to]] = [order[to], order[from]];
    this.update({ order });
  }

  setDensity(density: Density): void {
    this.update({ density });
  }

  setMotion(motion: boolean): void {
    this.update({ motion });
  }

  reset(): void {
    this.save(defaultLayout());
  }

  private update(change: Partial<Layout>): void {
    this.save({ ...this.layout(), ...change });
  }

  private save(layout: Layout): void {
    this.layout.set(layout);
    try {
      localStorage.setItem(KEY, JSON.stringify(layout));
    } catch {
      // Storage unavailable: the layout still applies until the app closes.
    }
  }

  private load(): Layout {
    try {
      const stored = localStorage.getItem(KEY);
      return normalize(stored ? JSON.parse(stored) : null);
    } catch {
      return defaultLayout();
    }
  }
}
