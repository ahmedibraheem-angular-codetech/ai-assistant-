import { Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LayoutService, WIDGETS, WidgetId } from '../core/layout';

/** Choose which widgets the home page shows, their order, and density. Saved at once. */
@Component({
  selector: 'app-customize-page',
  imports: [RouterLink],
  templateUrl: './customize-page.html',
  styleUrl: './customize-page.css',
})
export class CustomizePage {
  readonly layout = inject(LayoutService);

  readonly rows = computed(() => {
    const { order, hidden } = this.layout.layout();
    return order.map((id, index) => ({
      ...WIDGETS.find((w) => w.id === id)!,
      on: !hidden.includes(id),
      first: index === 0,
      last: index === order.length - 1,
    }));
  });

  readonly shownCount = computed(() => this.layout.visible().length);
  readonly total = WIDGETS.length;

  toggle(id: WidgetId): void {
    this.layout.toggle(id);
  }

  move(id: WidgetId, step: -1 | 1): void {
    this.layout.move(id, step);
  }
}
