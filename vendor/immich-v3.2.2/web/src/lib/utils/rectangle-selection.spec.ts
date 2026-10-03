import { describe, expect, it } from 'vitest';
import {
  createSelectionRectangle,
  findVerticalScrollport,
  getEdgeScrollVelocity,
  getIntersectingSelectionIds,
  selectionIntersects,
  type SelectionItem,
} from './rectangle-selection';

const assets: SelectionItem<string>[] = [
  { id: 'start', value: 'start', left: 0, top: 0, width: 100, height: 100 },
  { id: 'right', value: 'right', left: 100, top: 0, width: 100, height: 100 },
  { id: 'below', value: 'below', left: 0, top: 100, width: 100, height: 100 },
  { id: 'disabled', value: 'disabled', left: 100, top: 100, width: 100, height: 100, disabled: true },
  { id: 'far', value: 'far', left: 512, top: 512, width: 100, height: 100 },
];

describe('rectangle selection geometry', () => {
  it('normalizes drags in all four directions', () => {
    expect(createSelectionRectangle({ x: 40, y: 50 }, { x: 10, y: 5 })).toEqual({
      left: 10,
      top: 5,
      right: 40,
      bottom: 50,
    });
  });

  it('selects assets with positive-area overlap, including the starting asset', () => {
    expect(getIntersectingSelectionIds({ left: 90, top: 90, right: 110, bottom: 110 }, assets, 'start')).toEqual(
      new Set(['start', 'right', 'below']),
    );
  });

  it('does not count edge-only contact and ignores disabled assets', () => {
    expect(selectionIntersects({ left: 100, top: 0, right: 110, bottom: 100 }, assets[0])).toBe(false);
    expect(getIntersectingSelectionIds({ left: 100, top: 100, right: 150, bottom: 150 }, assets, 'start')).toEqual(
      new Set(['start']),
    );
  });

  it('limits intersection checks to vertical buckets touched by the selection', () => {
    expect(getIntersectingSelectionIds({ left: 500, top: 500, right: 620, bottom: 620 }, assets, 'far')).toEqual(
      new Set(['far']),
    );
  });
});

describe('rectangle selection edge scrolling', () => {
  it('accelerates toward the 32 pixel edges up to 480 pixels per second', () => {
    expect(getEdgeScrollVelocity(0, 0, 500)).toBe(-480);
    expect(getEdgeScrollVelocity(484, 0, 500)).toBe(120);
    expect(getEdgeScrollVelocity(250, 0, 500)).toBe(0);
  });
});

describe('rectangle selection scrollport', () => {
  it('uses the nearest scrollable ancestor before falling back to the document', () => {
    const container = document.createElement('div');
    container.style.overflowY = 'auto';
    Object.defineProperties(container, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 300 },
    });
    const grid = document.createElement('div');
    container.append(grid);
    document.body.append(container);

    expect(findVerticalScrollport(grid)).toBe(container);
    expect(findVerticalScrollport(document.createElement('div'))).toBe(document.scrollingElement);

    container.remove();
  });
});
