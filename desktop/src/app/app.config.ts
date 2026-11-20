import { ApplicationConfig, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding, withHashLocation } from '@angular/router';

import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    // Hash URLs: the bundled app is served from tauri:// with no server-side fallback,
    // so a reload on /chat/<id> must not ask for a file that does not exist.
    // Input binding: the `:id` route parameter arrives as the ChatPage `id` input.
    provideRouter(routes, withComponentInputBinding(), withHashLocation()),
  ],
};
