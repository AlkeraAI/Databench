// A viewport a test scrolls by hand. jsdom lays nothing out, so nothing ever
// intersects; this observer reports an element on screen only when the test
// says the reader scrolled to it.

import { act } from "@testing-library/react";
import { vi } from "vitest";

interface Watched {
  element: Element;
  observer: IntersectionObserver;
  callback: IntersectionObserverCallback;
}

/** Installs the hand-scrolled viewport; `vi.unstubAllGlobals()` removes it. */
export function handScrolledViewport(): { scrollTo(element: Element): void; watched(): Element[] } {
  let watched: Watched[] = [];

  class HandScrolledObserver {
    readonly root = null;
    readonly rootMargin = "0px";
    readonly thresholds = [0];

    private readonly callback: IntersectionObserverCallback;

    constructor(callback: IntersectionObserverCallback) {
      this.callback = callback;
    }

    observe(element: Element): void {
      watched.push({ element, observer: this as unknown as IntersectionObserver, callback: this.callback });
    }

    unobserve(element: Element): void {
      watched = watched.filter((one) => one.element !== element || one.observer !== (this as unknown as IntersectionObserver));
    }

    disconnect(): void {
      watched = watched.filter((one) => one.observer !== (this as unknown as IntersectionObserver));
    }

    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }

  vi.stubGlobal("IntersectionObserver", HandScrolledObserver);
  return {
    /** Brings every watched element inside `element` (or `element` itself) on screen. */
    scrollTo(element: Element): void {
      const hit = watched.filter((one) => element === one.element || element.contains(one.element));
      act(() => {
        for (const one of hit) {
          one.callback([{ isIntersecting: true, target: one.element } as IntersectionObserverEntry], one.observer);
        }
      });
    },
    watched: () => watched.map((one) => one.element),
  };
}
