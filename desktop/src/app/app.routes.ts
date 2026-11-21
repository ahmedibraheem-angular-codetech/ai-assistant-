import { Routes, UrlMatchResult, UrlSegment } from '@angular/router';

import { ApprovalsPage } from './approvals/approvals-page';
import { ChatPage } from './chat/chat-page';
import { CustomizePage } from './customize/customize-page';
import { DashboardPage } from './dashboard/dashboard-page';
import { KnowledgePage } from './knowledge/knowledge-page';
import { ProjectsPage } from './projects/projects-page';
import { SettingsPage } from './settings/settings-page';
import { AgentPage } from './team/agent-page';
import { TeamPage } from './team/team-page';

/**
 * Matches `/chat` and `/chat/<id>` with ONE route, so moving from a new chat to
 * the conversation it just created keeps the same ChatPage (no reload, no flicker).
 * Two separate routes would destroy and rebuild the page.
 */
export function chatMatcher(segments: UrlSegment[]): UrlMatchResult | null {
  if (segments[0]?.path !== 'chat' || segments.length > 2) return null;
  return {
    consumed: segments,
    posParams: segments[1] ? { id: segments[1] } : {},
  };
}

export const routes: Routes = [
  { path: '', pathMatch: 'full', component: DashboardPage, title: 'لوحة التحليلات' },
  { matcher: chatMatcher, component: ChatPage, title: 'المحادثة' },
  { path: 'customize', component: CustomizePage, title: 'تخصيص الرئيسية' },
  { path: 'approvals', component: ApprovalsPage, title: 'الموافقات' },
  { path: 'routines', loadComponent: () => import('./routines/routines-page').then((m) => m.RoutinesPage), title: 'المهام المجدولة' },
  { path: 'style', loadComponent: () => import('./style/style-page').then((m) => m.StylePage), title: 'أسلوبي' },
  { path: 'settings', component: SettingsPage, title: 'الإعدادات' },
  { path: 'team', component: TeamPage, title: 'الفريق' },
  { path: 'team/:id', component: AgentPage, title: 'ملف الوكيل' },
  { path: 'projects', component: ProjectsPage, title: 'المشاريع' },
  { path: 'knowledge', component: KnowledgePage, title: 'خريطة المعرفة' },
  { path: '**', redirectTo: '' },
];
