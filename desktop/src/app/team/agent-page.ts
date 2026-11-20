import { Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { AssistantApi } from '../core/api';
import { Agent, AgentProfile, Lesson, Skill, StudyPlan } from '../core/models';
import { ago } from '../dashboard/dashboard-view';
import {
  evalDecision,
  intervalLabel,
  lessonSource,
  parseTopics,
  score,
  skillStatus,
  until,
  usage,
  usageRatio,
  waitingCount,
} from './team-view';

const INTERVALS = [6, 12, 24, 48, 72, 168];

@Component({
  selector: 'app-agent-page',
  imports: [FormsModule, RouterLink],
  templateUrl: './agent-page.html',
  styleUrl: './team.css',
})
export class AgentPage {
  private readonly api = inject(AssistantApi);

  /** Route parameter `/team/:id`. */
  readonly id = input.required<string>();

  readonly agent = signal<Agent | null>(null);
  readonly others = signal<Agent[]>([]);
  readonly profile = signal<AgentProfile | null>(null);
  readonly plan = signal<StudyPlan | null>(null);
  readonly members = signal<string[]>([]);
  readonly error = signal('');
  readonly notice = signal('');
  readonly busy = signal<string | null>(null);
  readonly openSkill = signal<string | null>(null);

  readonly topicsText = signal('');
  readonly interval = signal(24);
  readonly intervals = INTERVALS;

  readonly waiting = computed(() => {
    const p = this.profile();
    return p ? waitingCount(p) : 0;
  });

  readonly usage = usage;
  readonly usageRatio = usageRatio;
  readonly lessonSource = lessonSource;
  readonly skillStatus = skillStatus;
  readonly evalDecision = evalDecision;
  readonly score = score;
  readonly intervalLabel = intervalLabel;
  readonly ago = ago;
  readonly until = until;

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
    });
  }

  async load(id = this.id()): Promise<void> {
    try {
      const [list, profile, plan, team] = await Promise.all([
        this.api.agents(),
        this.api.profile(id),
        this.api.studyPlan(id),
        this.api.team(id),
      ]);
      this.agent.set(list.agents.find((a) => a.id === id) ?? null);
      this.others.set(list.agents.filter((a) => a.id !== id));
      this.profile.set(profile);
      this.plan.set(plan);
      this.members.set(team.member_ids);
      this.topicsText.set(plan ? plan.topics.join('\n') : '');
      this.interval.set(plan ? plan.interval_hours : 24);
      this.error.set('');
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    }
  }

  private async act(key: string, action: () => Promise<unknown>, done: string): Promise<void> {
    if (this.busy()) return;
    this.busy.set(key);
    this.error.set('');
    this.notice.set('');
    try {
      await action();
      this.notice.set(done);
      await this.load();
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(null);
    }
  }

  toggleEnabled(): Promise<void> {
    const agent = this.agent();
    if (!agent) return Promise.resolve();
    return this.act('enabled', () => this.api.setAgentEnabled(agent.id, !agent.enabled),
      agent.enabled ? 'أوقفت الوكيل. لن يرد ولن يستخدم أدوات حتى تشغّله.' : 'شغّلت الوكيل.');
  }

  lesson(item: Lesson, status: Lesson['status']): Promise<void> {
    const done = status === 'active' ? 'اعتمدت الدرس. سيتذكره الوكيل.' : 'استبعدت الدرس.';
    return this.act(`lesson:${item.id}`, () => this.api.setLessonStatus(item.id, status), done);
  }

  skill(item: Skill, status: 'active' | 'rejected' | 'retired'): Promise<void> {
    const done = { active: 'فعّلت المهارة.', rejected: 'رفضت المهارة.', retired: 'أوقفت المهارة.' }[status];
    return this.act(`skill:${item.id}`, () => this.api.setSkillStatus(item.id, status), done);
  }

  isMember(id: string): boolean {
    return this.members().includes(id);
  }

  toggleMember(id: string): Promise<void> {
    const next = this.isMember(id) ? this.members().filter((m) => m !== id) : [...this.members(), id];
    return this.act('team', () => this.api.setTeam(this.id(), next), 'حدّثت الفريق.');
  }

  savePlan(): Promise<void> {
    const topics = parseTopics(this.topicsText());
    if (!topics.length) {
      this.error.set('اكتب موضوعاً واحداً على الأقل.');
      return Promise.resolve();
    }
    return this.act('plan', () => this.api.setStudyPlan(this.id(), topics, this.interval()), 'حفظت خطة المذاكرة.');
  }

  studyNow(): Promise<void> {
    return this.act('study', () => this.api.studyNow(this.id()),
      'انتهت جلسة المذاكرة. أي دروس جديدة من الويب تظهر بالأعلى بانتظار مراجعتك.');
  }
}
