import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';

import { AssistantApi } from '../core/api';
import { Agent, AgentActivity, AgentTemplate, HireResult, StudyDigest } from '../core/models';
import { ago } from '../dashboard/dashboard-view';
import { activityLine, budgetLabel } from './team-view';

@Component({
  selector: 'app-team-page',
  imports: [FormsModule, RouterLink],
  templateUrl: './team-page.html',
  styleUrl: './team.css',
})
export class TeamPage {
  private readonly api = inject(AssistantApi);
  private readonly router = inject(Router);

  readonly agents = signal<Agent[]>([]);
  readonly activity = signal<Record<string, AgentActivity>>({});
  readonly templates = signal<AgentTemplate[]>([]);
  readonly digest = signal<StudyDigest | null>(null);
  readonly ago = ago;
  readonly loading = signal(true);
  readonly error = signal('');

  readonly hiring = signal(false);
  readonly chosen = signal<AgentTemplate | null>(null);
  readonly name = signal('');
  readonly instructions = signal('');
  readonly applyBudget = signal(true);
  readonly busy = signal(false);
  readonly hired = signal<HireResult | null>(null);

  readonly sorted = computed(() =>
    [...this.agents()].sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.name.localeCompare(b.name, 'ar')),
  );

  readonly activityLine = (id: string) => activityLine(this.activity()[id]);
  readonly budgetLabel = budgetLabel;

  constructor() {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const [list, summary, digest] = await Promise.all([
        this.api.agents(),
        this.api.analytics('today'),
        this.api.studyDigest(24).catch(() => null),
      ]);
      this.agents.set(list.agents);
      this.digest.set(digest);
      this.activity.set(Object.fromEntries(summary.agents.map((a) => [a.id, a])));
      this.error.set('');
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.loading.set(false);
    }
  }

  async openHiring(): Promise<void> {
    this.hiring.set(true);
    this.hired.set(null);
    if (!this.templates().length) {
      try {
        this.templates.set((await this.api.templates()).templates);
      } catch (error) {
        this.error.set(error instanceof Error ? error.message : String(error));
      }
    }
  }

  choose(template: AgentTemplate): void {
    this.chosen.set(template);
    this.name.set(template.title_ar);
    this.instructions.set('');
    this.applyBudget.set(true);
  }

  closeHiring(): void {
    this.hiring.set(false);
    this.chosen.set(null);
  }

  async hire(): Promise<void> {
    const template = this.chosen();
    if (!template || this.busy()) return;
    this.busy.set(true);
    this.error.set('');
    try {
      const result = await this.api.hire({
        template_id: template.id,
        name: this.name().trim() || undefined,
        extra_instructions: this.instructions().trim() || undefined,
        apply_budget: this.applyBudget(),
      });
      this.hired.set(result);
      this.chosen.set(null);
      await this.load();
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(false);
    }
  }

  agentName(id: string): string {
    return this.agents().find((a) => a.id === id)?.name ?? 'وكيل';
  }

  openProfile(id: string): void {
    void this.router.navigate(['/team', id]);
  }
}
