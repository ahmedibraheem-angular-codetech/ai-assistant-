import { Injectable, InjectionToken, inject } from '@angular/core';
import { isTauri } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';

import { TAURI_INVOKE } from './api';

/** Which window this page runs in: "main" or "quick" (the ⌥ Space window). */
export const WINDOW_LABEL = new InjectionToken<string>('WINDOW_LABEL', {
  providedIn: 'root',
  factory: () => {
    try {
      return isTauri() ? getCurrentWindow().label : 'main';
    } catch {
      return 'main';
    }
  },
});

/** The two desktop commands the quick window may call (see src-tauri/src/desktop.rs). */
@Injectable({ providedIn: 'root' })
export class DesktopBridge {
  private readonly invoke = inject(TAURI_INVOKE);

  hideQuick(): Promise<void> {
    return this.invoke<void>('hide_quick').catch(() => undefined);
  }

  /** Opens an allowed page in the main window and hides the quick window. */
  openMain(path: string): Promise<void> {
    return this.invoke<void>('open_main', { path });
  }
}
