import { Routes, UrlMatchResult, UrlSegment } from '@angular/router';

import { ChatPage } from './chat/chat-page';
import { DashboardPage } from './dashboard/dashboard-page';

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
  { path: '**', redirectTo: '' },
];
