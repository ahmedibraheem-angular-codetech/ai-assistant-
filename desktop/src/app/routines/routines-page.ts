import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssistantApi } from '../core/api';
import { Agent, Routine } from '../core/models';
import { DAY_CHIPS, MORNING_PRESET, daysText, statusText, statusTone, toggleDay, validateRoutine } from './routines-view';

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

@Component({
  selector: 'app-routines-page',
  imports: [RouterLink],
  templateUrl: './routines-page.html',
  styleUrl: './routines-page.css',
})
export class RoutinesPage {
  private readonly api = inject(AssistantApi);

  readonly routines = signal<Routine[]>([]);
  readonly agents = signal<Agent[]>([]);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly busy = signal('');
  readonly confirmDelete = signal<string | null>(null);

  // Form: editing === null means closed, '' means a new routine.
  readonly editing = signal<string | null>(null);
  readonly name = signal('');
  readonly prompt = signal('');
  readonly agentId = signal('');
  readonly time = signal('08:00');
  readonly days = signal<number[]>([]);
  readonly formError = signal('');

  readonly names = computed(() => Object.fromEntries(this.agents().map((a) => [a.id, a.name])));
  readonly chips = DAY_CHIPS;
  readonly daysText = daysText;
  readonly statusText = statusText;
  readonly statusTone = statusTone;

  constructor() {
    void this.load();
  }

  agentName(id: string | null): string {
    return id ? this.names()[id] || 'وكيل محذوف' : 'المساعد الافتراضي';
  }

  private async load(): Promise<void> {
    try {
      const [{ routines }, { agents }] = await Promise.all([this.api.routines(), this.api.agents()]);
      this.routines.set(routines);
      this.agents.set(agents);
    } catch (e) {
      this.error.set(message(e));
    } finally {
      this.loading.set(false);
    }
  }

  openNew(): void {
    this.fill('', '', '', '08:00', []);
  }

  preset(): void {
    this.fill(this.editing() ?? '', MORNING_PRESET.name, this.agentId(), MORNING_PRESET.time, [], MORNING_PRESET.prompt);
  }

  edit(r: Routine): void {
    this.fill(r.id, r.name, r.agent_id ?? '', r.time_of_day, r.days, r.prompt);
  }

  private fill(id: string, name: string, agent: string, time: string, days: number[], prompt = ''): void {
    this.editing.set(id);
    this.name.set(name);
    this.agentId.set(agent);
    this.time.set(time);
    this.days.set(days);
    this.prompt.set(prompt);
    this.formError.set('');
  }

  cancel(): void {
    this.editing.set(null);
  }

  toggleDay(day: number): void {
    this.days.update((d) => toggleDay(d, day));
  }

  async save(): Promise<void> {
    const id = this.editing();
    if (id === null || this.busy()) return;
    const check = validateRoutine(this.name(), this.prompt(), this.time());
    if (!check.ok) return this.formError.set(check.error);
    const body = {
      name: this.name().trim(), prompt: this.prompt().trim(), agent_id: this.agentId() || null,
      time_of_day: this.time(), days: this.days(),
    };
    await this.act('save', async () => {
      const saved = id ? await this.api.updateRoutine(id, body) : await this.api.createRoutine(body);
      this.replace(saved);
      this.editing.set(null);
    }, true);
  }

  async toggle(r: Routine): Promise<void> {
    if (this.busy()) return;
    await this.act(r.id, async () => this.replace(await this.api.updateRoutine(r.id, { enabled: !r.enabled })));
  }

  async run(r: Routine): Promise<void> {
    if (this.busy()) return;
    await this.act(r.id, async () => this.replace(await this.api.runRoutine(r.id)));
  }

  async remove(r: Routine): Promise<void> {
    if (this.busy()) return;
    if (this.confirmDelete() !== r.id) return this.confirmDelete.set(r.id);
    await this.act(r.id, async () => {
      await this.api.deleteRoutine(r.id);
      this.routines.update((all) => all.filter((x) => x.id !== r.id));
      this.confirmDelete.set(null);
    });
  }

  private replace(r: Routine): void {
    this.routines.update((all) => (all.some((x) => x.id === r.id) ? all.map((x) => (x.id === r.id ? r : x)) : [...all, r]));
  }

  private async act(key: string, action: () => Promise<void>, inForm = false): Promise<void> {
    this.busy.set(key);
    this.error.set('');
    this.formError.set('');
    try {
      await action();
    } catch (e) {
      (inForm ? this.formError : this.error).set(message(e));
    } finally {
      this.busy.set('');
    }
  }
}
