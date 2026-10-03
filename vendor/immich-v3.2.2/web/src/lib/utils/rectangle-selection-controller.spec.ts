import { rectangleSelection, type RectangleSelectionOptions, type SelectionItem } from './rectangle-selection';

function pointerEvent(type: string, { x, y, shiftKey = false }: { x: number; y: number; shiftKey?: boolean }) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { button: 0, pointerId: 1, pointerType: 'mouse', clientX: x, clientY: y, shiftKey });
  return event;
}

describe('rectangle selection controller', () => {
  let grid: HTMLElement;
  let cancel: () => void;
  let selection: ReturnType<typeof rectangleSelection<string>> | undefined;

  const items: SelectionItem<string>[] = [
    { id: 'one', value: 'one', left: 0, top: 0, width: 100, height: 100 },
    { id: 'two', value: 'two', left: 100, top: 0, width: 100, height: 100 },
    { id: 'three', value: 'three', left: 0, top: 100, width: 100, height: 100 },
    { id: 'four', value: 'four', left: 100, top: 100, width: 100, height: 100 },
  ];

  const setup = (
    selected: string[] = [],
    selectionItems: SelectionItem<string>[] = items,
    getScrollport: () => HTMLElement | null = () => null,
  ) => {
    grid = document.createElement('div');
    const start = document.createElement('div');
    start.dataset.asset = 'one';
    grid.append(start);
    document.body.append(grid);

    const onChange = vi.fn();
    const onCommit = vi.fn();
    const options: RectangleSelectionOptions<string> = {
      enabled: () => true,
      getItems: () => selectionItems,
      getOrigin: () => ({ left: 0, top: 0 }),
      getScrollport,
      getSelectedAssets: () => selected,
      getId: (asset) => asset,
      onChange,
      onCommit,
      onControllerReady: (handler) => (cancel = handler),
    };
    selection = rectangleSelection(grid, options);
    return { start, onChange, onCommit };
  };

  afterEach(() => {
    selection?.destroy();
    selection = undefined;
    grid?.remove();
    vi.unstubAllGlobals();
  });

  it('replaces selection on a normal drag and suppresses its trailing click', () => {
    const { start, onCommit } = setup(['old']);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 150, y: 150 }));
    document.dispatchEvent(pointerEvent('pointerup', { x: 150, y: 150 }));

    expect(onCommit).toHaveBeenCalledWith(expect.arrayContaining(['one', 'two', 'three', 'four']), 'one', false);
    expect(onCommit.mock.calls[0][0]).not.toContain('old');

    const trailingClick = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
    start.dispatchEvent(trailingClick);
    expect(trailingClick.defaultPrevented).toBe(true);
  });

  it('captures Shift at pointer down and keeps the original selection while the rectangle shrinks', () => {
    const { start, onChange, onCommit } = setup(['old']);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20, shiftKey: true }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 150, y: 150 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 70, y: 70 }));
    document.dispatchEvent(pointerEvent('pointerup', { x: 70, y: 70 }));

    expect(onChange.mock.calls.at(-2)?.[0]?.selectedIds).toEqual(new Set(['old', 'one']));
    expect(onCommit).toHaveBeenCalledWith(['old', 'one'], 'one', true);
  });

  it('leaves a below-threshold pointer sequence as an ordinary image click', () => {
    const { start, onChange, onCommit } = setup();
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 24, y: 22 }));
    document.dispatchEvent(pointerEvent('pointerup', { x: 24, y: 22 }));

    const click = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
    start.dispatchEvent(click);
    expect(onCommit).not.toHaveBeenCalled();
    expect(click.defaultPrevented).toBe(false);
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('cancels without committing when the active gesture is cancelled', () => {
    const { start, onChange, onCommit } = setup();
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    cancel();

    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('cancels and restores the original selection on Escape', () => {
    const { start, onChange, onCommit } = setup(['old']);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));

    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    document.dispatchEvent(escape);
    document.dispatchEvent(pointerEvent('pointerup', { x: 60, y: 20 }));

    expect(escape.defaultPrevented).toBe(true);
    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('cancels on pointercancel without committing a partial selection', () => {
    const { start, onChange, onCommit } = setup(['old']);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    document.dispatchEvent(pointerEvent('pointercancel', { x: 60, y: 20 }));

    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('cancels on window blur and suppresses the pointerup click', () => {
    const { start, onChange, onCommit } = setup(['old']);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    dispatchEvent(new Event('blur'));
    document.dispatchEvent(pointerEvent('pointerup', { x: 60, y: 20 }));

    const trailingClick = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
    start.dispatchEvent(trailingClick);
    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
    expect(trailingClick.defaultPrevented).toBe(true);
  });

  it('does not start rectangle selection from a disabled asset', () => {
    const { start, onChange, onCommit } = setup();
    start.dataset.disabled = 'true';
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    document.dispatchEvent(pointerEvent('pointerup', { x: 60, y: 20 }));

    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('auto-scrolls the active scrollport while dragging at its edge', () => {
    let frame: FrameRequestCallback | undefined;
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      frame = callback;
      return requestFrame.mock.calls.length;
    });
    vi.stubGlobal('requestAnimationFrame', requestFrame);
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    let scrollTop = 0;
    const scrollport = document.createElement('div');
    Object.defineProperties(scrollport, {
      clientHeight: { configurable: true, value: 100 },
      clientTop: { configurable: true, value: 0 },
      scrollHeight: { configurable: true, value: 500 },
      scrollTop: { configurable: true, get: () => scrollTop, set: (value) => (scrollTop = value) },
    });
    scrollport.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      top: 0,
      right: 100,
      bottom: 100,
      left: 0,
      width: 100,
      height: 100,
      toJSON: () => ({}),
    });
    scrollport.scrollBy = vi.fn(({ top }: ScrollToOptions) => {
      scrollTop = Math.max(0, Math.min(400, scrollTop + Number(top ?? 0)));
    });

    const { start } = setup([], items, () => scrollport);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 99 }));
    frame?.(16);

    expect(scrollport.scrollBy).toHaveBeenCalledOnce();
    expect(scrollTop).toBeGreaterThan(0);
    expect(requestFrame).toHaveBeenCalledTimes(2);
  });

  it('suppresses the click that follows a cancelled drag', () => {
    const { start, onCommit } = setup();
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    cancel();
    document.dispatchEvent(pointerEvent('pointerup', { x: 60, y: 20 }));

    const trailingClick = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
    start.dispatchEvent(trailingClick);
    expect(onCommit).not.toHaveBeenCalled();
    expect(trailingClick.defaultPrevented).toBe(true);
  });

  it('stops the animation loop when the scrollport cannot move', () => {
    let frame: FrameRequestCallback | undefined;
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal('requestAnimationFrame', requestFrame);
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    const scrollport = document.createElement('div');
    Object.defineProperties(scrollport, {
      clientHeight: { configurable: true, value: 100 },
      scrollTop: { configurable: true, value: 0, writable: true },
    });
    scrollport.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      top: 0,
      right: 100,
      bottom: 100,
      left: 0,
      width: 100,
      height: 100,
      toJSON: () => ({}),
    });
    scrollport.scrollBy = vi.fn();

    const { start } = setup([], items, () => scrollport);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 40, y: 0 }));
    frame?.(16);

    expect(scrollport.scrollBy).toHaveBeenCalledOnce();
    expect(requestFrame).toHaveBeenCalledOnce();
  });

  it('cancels if a style mutation changes the active layout', async () => {
    let frame: FrameRequestCallback | undefined;
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    const selectionItems = items.map((item) => ({ ...item }));
    const { start, onChange, onCommit } = setup([], selectionItems);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 60, y: 20 }));
    selectionItems[0] = { ...selectionItems[0], left: 20 };
    start.style.left = '20px';
    await Promise.resolve();
    frame?.(16);

    expect(onCommit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('includes assets added by virtual loading while the gesture remains active', async () => {
    let frame: FrameRequestCallback | undefined;
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    const selectionItems = items.map((item) => ({ ...item }));
    const { start, onChange, onCommit } = setup([], selectionItems);
    start.dispatchEvent(pointerEvent('pointerdown', { x: 20, y: 20 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 40, y: 40 }));
    document.dispatchEvent(pointerEvent('pointermove', { x: 80, y: 80 }));

    const loadedAsset = document.createElement('div');
    loadedAsset.dataset.asset = 'loaded';
    grid.append(loadedAsset);
    selectionItems.push({ id: 'loaded', value: 'loaded', left: 60, top: 60, width: 100, height: 100 });
    await Promise.resolve();
    frame?.(16);

    expect(onChange.mock.calls.at(-1)?.[0]?.selectedIds).toContain('loaded');
    document.dispatchEvent(pointerEvent('pointerup', { x: 80, y: 80 }));
    expect(onCommit.mock.calls.at(-1)?.[0]).toContain('loaded');
  });
});
