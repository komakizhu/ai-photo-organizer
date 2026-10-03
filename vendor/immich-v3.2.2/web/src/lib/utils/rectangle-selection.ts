export type SelectionPoint = { x: number; y: number };

export type SelectionRectangle = {
  left: number;
  top: number;
  right: number;
  bottom: number;
};

export type SelectionItem<T> = {
  id: string;
  value: T;
  left: number;
  top: number;
  width: number;
  height: number;
  disabled?: boolean;
};

export type SelectionOrigin = { left: number; top: number };

export type RectangleSelectionUpdate = {
  rectangle: SelectionRectangle;
  selectedIds: Set<string>;
};

export type RectangleSelectionOptions<T> = {
  enabled: () => boolean;
  getItems: () => SelectionItem<T>[];
  getOrigin: () => SelectionOrigin;
  getScrollport: () => HTMLElement | null;
  getSelectedAssets: () => T[];
  getId: (asset: T) => string;
  onStart?: () => void;
  onChange: (update: RectangleSelectionUpdate | null) => void;
  onCommit: (assets: T[], startAsset: T, additive: boolean) => void;
  onCancel?: () => void;
  onControllerReady?: (cancel: () => void) => void;
};

const DRAG_THRESHOLD = 6;
const EDGE_SIZE = 32;
const MAX_SCROLL_SPEED = 480;
const BUCKET_HEIGHT = 256;

export function createSelectionRectangle(start: SelectionPoint, end: SelectionPoint): SelectionRectangle {
  return {
    left: Math.min(start.x, end.x),
    top: Math.min(start.y, end.y),
    right: Math.max(start.x, end.x),
    bottom: Math.max(start.y, end.y),
  };
}

export function selectionIntersects(
  rectangle: SelectionRectangle,
  item: Pick<SelectionItem<unknown>, 'left' | 'top' | 'width' | 'height'>,
) {
  return (
    Math.min(rectangle.right, item.left + item.width) > Math.max(rectangle.left, item.left) &&
    Math.min(rectangle.bottom, item.top + item.height) > Math.max(rectangle.top, item.top)
  );
}

export function getIntersectingSelectionIds<T>(
  rectangle: SelectionRectangle,
  items: SelectionItem<T>[],
  startingAssetId: string,
) {
  return getIntersectingSelectionIdsFromBuckets(rectangle, itemsByVerticalBucket(items), startingAssetId);
}

function getIntersectingSelectionIdsFromBuckets<T>(
  rectangle: SelectionRectangle,
  buckets: Map<number, SelectionItem<T>[]>,
  startingAssetId: string,
) {
  const result = new Set<string>();
  const firstBucket = Math.floor(rectangle.top / BUCKET_HEIGHT);
  const lastBucket = Math.floor(rectangle.bottom / BUCKET_HEIGHT);
  const seen = new Set<SelectionItem<T>>();

  for (let bucket = firstBucket; bucket <= lastBucket; bucket++) {
    for (const item of buckets.get(bucket) ?? []) {
      if (seen.has(item)) {
        continue;
      }
      seen.add(item);
      if (!item.disabled && selectionIntersects(rectangle, item)) {
        result.add(item.id);
      }
    }
  }

  result.add(startingAssetId);
  return result;
}

function itemsByVerticalBucket<T>(items: SelectionItem<T>[]) {
  const buckets = new Map<number, SelectionItem<T>[]>();
  for (const item of items) {
    const first = Math.floor(item.top / BUCKET_HEIGHT);
    const last = Math.floor((item.top + item.height) / BUCKET_HEIGHT);
    for (let bucket = first; bucket <= last; bucket++) {
      const bucketItems = buckets.get(bucket) ?? [];
      bucketItems.push(item);
      buckets.set(bucket, bucketItems);
    }
  }
  return buckets;
}

export function getEdgeScrollVelocity(pointerY: number, top: number, bottom: number, edgeSize = EDGE_SIZE) {
  if (pointerY < top + edgeSize) {
    const progress = Math.max(0, Math.min(1, (top + edgeSize - pointerY) / edgeSize));
    return -MAX_SCROLL_SPEED * progress ** 2;
  }
  if (pointerY > bottom - edgeSize) {
    const progress = Math.max(0, Math.min(1, (pointerY - (bottom - edgeSize)) / edgeSize));
    return MAX_SCROLL_SPEED * progress ** 2;
  }
  return 0;
}

export function findVerticalScrollport(node: HTMLElement | undefined): HTMLElement | null {
  let ancestor = node?.parentElement;
  while (ancestor && ancestor !== document.body && ancestor !== document.documentElement) {
    const { overflowY } = getComputedStyle(ancestor);
    if (/(auto|scroll|overlay)/.test(overflowY) && ancestor.scrollHeight > ancestor.clientHeight) {
      return ancestor;
    }
    ancestor = ancestor.parentElement;
  }
  return document.scrollingElement as HTMLElement | null;
}

/** Shared mouse/trackpad rectangle-selection gesture for virtualized asset grids. */
export function rectangleSelection<T>(node: HTMLElement, options: RectangleSelectionOptions<T>) {
  let currentOptions = options;
  let pointerId: number | null = null;
  let startPoint: SelectionPoint | null = null;
  let lastPoint: SelectionPoint | null = null;
  let startItem: SelectionItem<T> | null = null;
  let items: SelectionItem<T>[] = [];
  let itemById = new Map<string, SelectionItem<T>>();
  let originalSelection: T[] = [];
  let additive = false;
  let active = false;
  let dirty = false;
  let cancelledPointerId: number | null = null;
  let animationFrame = 0;
  let previousFrameTime = 0;
  let suppressNextClick = false;
  let suppressionTimer: ReturnType<typeof setTimeout> | undefined;

  const buckets = () => itemsByVerticalBucket(items);
  let verticalBuckets = new Map<number, SelectionItem<T>[]>();

  const suppressFollowingClick = () => {
    suppressNextClick = true;
    if (suppressionTimer) {
      clearTimeout(suppressionTimer);
    }
    suppressionTimer = setTimeout(() => (suppressNextClick = false), 0);
  };

  const stopAnimation = () => {
    if (animationFrame) {
      cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }
    previousFrameTime = 0;
  };

  const resetGesture = () => {
    stopAnimation();
    const capturedPointerId = pointerId;
    pointerId = null;
    if (capturedPointerId !== null && node.hasPointerCapture?.(capturedPointerId)) {
      try {
        node.releasePointerCapture?.(capturedPointerId);
      } catch {
        // The browser may already have released the pointer after pointerup or blur.
      }
    }
    startPoint = null;
    lastPoint = null;
    startItem = null;
    items = [];
    itemById.clear();
    originalSelection = [];
    additive = false;
    active = false;
    dirty = false;
    previousFrameTime = 0;
    verticalBuckets = new Map();
    currentOptions.onChange(null);
  };

  const refreshItems = () => {
    const refreshedItems = currentOptions
      .getItems()
      .filter((item) => !item.disabled && item.width > 0 && item.height > 0);
    const previousItems = new Map(items.map((item) => [item.id, item]));
    const layoutChanged = refreshedItems.some((item) => {
      const previous = previousItems.get(item.id);
      return (
        previous &&
        (Math.abs(previous.left - item.left) > 0.1 ||
          Math.abs(previous.top - item.top) > 0.1 ||
          Math.abs(previous.width - item.width) > 0.1 ||
          Math.abs(previous.height - item.height) > 0.1)
      );
    });
    if (layoutChanged) {
      cancelGesture();
      return false;
    }
    items = refreshedItems;
    itemById = new Map(items.map((item) => [item.id, item]));
    verticalBuckets = buckets();
    dirty = false;
    const refreshedStartItem = startItem ? items.find((item) => item.id === startItem?.id) : undefined;
    if (startItem && !refreshedStartItem) {
      cancelGesture();
      return false;
    }
    startItem = refreshedStartItem ?? startItem;
    return true;
  };

  const cancelGesture = () => {
    if (pointerId === null) {
      return;
    }
    cancelledPointerId = pointerId;
    resetGesture();
    currentOptions.onCancel?.();
  };

  const updateSelection = (point: SelectionPoint) => {
    if (!startPoint || !startItem) {
      return;
    }
    const rectangle = createSelectionRectangle(startPoint, point);
    const origin = currentOptions.getOrigin();
    const relativeRectangle: SelectionRectangle = {
      left: rectangle.left - origin.left,
      top: rectangle.top - origin.top,
      right: rectangle.right - origin.left,
      bottom: rectangle.bottom - origin.top,
    };
    const selectedIds = getIntersectingSelectionIdsFromBuckets(relativeRectangle, verticalBuckets, startItem.id);
    const selectedAssets = additive ? [...originalSelection] : [];
    const assetById = new Map(selectedAssets.map((asset) => [currentOptions.getId(asset), asset]));

    for (const id of selectedIds) {
      const item = itemById.get(id);
      if (item) {
        assetById.set(item.id, item.value);
      }
    }

    currentOptions.onChange({ rectangle, selectedIds: new Set(assetById.keys()) });
    lastPoint = point;
  };

  const animate = (now: number) => {
    if (!active || !lastPoint) {
      animationFrame = 0;
      previousFrameTime = 0;
      return;
    }
    if (dirty && !refreshItems()) {
      animationFrame = 0;
      return;
    }

    const scrollport = currentOptions.getScrollport();
    if (!scrollport) {
      animationFrame = 0;
      previousFrameTime = 0;
      updateSelection(lastPoint);
      return;
    }

    const isDocumentScrollport = scrollport === document.scrollingElement;
    const scrollportRect = isDocumentScrollport ? undefined : scrollport.getBoundingClientRect();
    const top = isDocumentScrollport ? 0 : (scrollportRect?.top ?? 0) + scrollport.clientTop;
    const bottom = isDocumentScrollport ? window.innerHeight : top + scrollport.clientHeight;
    const velocity = getEdgeScrollVelocity(lastPoint.y, top, bottom);
    if (velocity === 0) {
      animationFrame = 0;
      previousFrameTime = 0;
      updateSelection(lastPoint);
      return;
    }

    const elapsed = previousFrameTime === 0 ? 16 : Math.min(50, now - previousFrameTime);
    const previousScrollTop = scrollport.scrollTop;
    scrollport.scrollBy({ top: (velocity * elapsed) / 1000 });
    const didScroll = Math.abs(scrollport.scrollTop - previousScrollTop) > 0.01;
    previousFrameTime = now;
    updateSelection(lastPoint);
    if (!didScroll) {
      animationFrame = 0;
      previousFrameTime = 0;
      return;
    }
    animationFrame = requestAnimationFrame(animate);
  };

  const startAnimation = () => {
    if (animationFrame) {
      return;
    }
    previousFrameTime = 0;
    animationFrame = requestAnimationFrame(animate);
  };

  const onPointerDown = (event: PointerEvent) => {
    if (!currentOptions.enabled() || event.button !== 0 || event.pointerType === 'touch' || pointerId !== null) {
      return;
    }
    if (cancelledPointerId !== null) {
      cancelledPointerId = null;
    }
    if ((event.target as Element | null)?.closest('button, input, [data-no-drag-select]')) {
      return;
    }
    const target = (event.target as Element | null)?.closest<HTMLElement>('[data-asset]');
    if (!target || target.dataset.disabled === 'true' || target.dataset.readonly === 'true') {
      return;
    }
    items = currentOptions.getItems().filter((item) => !item.disabled && item.width > 0 && item.height > 0);
    itemById = new Map(items.map((item) => [item.id, item]));
    const start = items.find((item) => item.id === target.dataset.asset);
    if (!start) {
      return;
    }

    pointerId = event.pointerId;
    startPoint = { x: event.clientX, y: event.clientY };
    lastPoint = startPoint;
    startItem = start;
    verticalBuckets = buckets();
    originalSelection = currentOptions.getSelectedAssets();
    additive = event.shiftKey;
    active = false;
  };

  const onPointerMove = (event: PointerEvent) => {
    if (pointerId !== event.pointerId || !startPoint || !startItem) {
      return;
    }
    lastPoint = { x: event.clientX, y: event.clientY };
    if (!active) {
      const distance = Math.hypot(lastPoint.x - startPoint.x, lastPoint.y - startPoint.y);
      if (distance <= DRAG_THRESHOLD) {
        return;
      }
      active = true;
      currentOptions.onStart?.();
      try {
        node.setPointerCapture(event.pointerId);
      } catch {
        // Document-level listeners still finish the gesture when pointer capture is unavailable.
      }
      event.preventDefault();
      updateSelection(lastPoint);
      startAnimation();
      return;
    }
    event.preventDefault();
    updateSelection(lastPoint);
    startAnimation();
  };

  const onPointerUp = (event: PointerEvent) => {
    if (cancelledPointerId === event.pointerId) {
      cancelledPointerId = null;
      suppressFollowingClick();
      return;
    }
    if (pointerId !== event.pointerId) {
      return;
    }
    if (active && startItem) {
      if (dirty && !refreshItems()) {
        if (cancelledPointerId === event.pointerId) {
          cancelledPointerId = null;
          suppressFollowingClick();
        }
        return;
      }
      const rectangle = createSelectionRectangle(startPoint!, { x: event.clientX, y: event.clientY });
      const origin = currentOptions.getOrigin();
      const selectedIds = getIntersectingSelectionIdsFromBuckets(
        {
          left: rectangle.left - origin.left,
          top: rectangle.top - origin.top,
          right: rectangle.right - origin.left,
          bottom: rectangle.bottom - origin.top,
        },
        verticalBuckets,
        startItem.id,
      );
      const selectedAssets = additive ? [...originalSelection] : [];
      const assetById = new Map(selectedAssets.map((asset) => [currentOptions.getId(asset), asset]));
      for (const id of selectedIds) {
        const item = itemById.get(id);
        if (item) {
          assetById.set(item.id, item.value);
        }
      }
      suppressFollowingClick();
      currentOptions.onCommit([...assetById.values()], startItem.value, additive);
    }
    resetGesture();
  };

  const onPointerCancel = (event: PointerEvent) => {
    if (cancelledPointerId === event.pointerId) {
      cancelledPointerId = null;
      return;
    }
    if (pointerId === event.pointerId) {
      cancelGesture();
      cancelledPointerId = null;
    }
  };

  const onLostPointerCapture = (event: PointerEvent) => {
    if (pointerId === event.pointerId) {
      cancelGesture();
    }
  };

  const onClickCapture = (event: MouseEvent) => {
    if (!suppressNextClick || event.detail === 0) {
      return;
    }
    suppressNextClick = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (pointerId === null) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      cancelGesture();
    } else if (event.key !== 'Shift' && event.key !== 'Control' && event.key !== 'Meta' && event.key !== 'Alt') {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };

  const onBlur = () => cancelGesture();
  const onVisibilityChange = () => {
    if (document.visibilityState === 'hidden') {
      cancelGesture();
    }
  };

  const resizeObserver = new ResizeObserver(() => {
    if (pointerId === null) {
      return;
    }
    dirty = true;
    if (active) {
      startAnimation();
    } else {
      refreshItems();
    }
  });
  resizeObserver.observe(node);

  const mutationObserver = new MutationObserver((records) => {
    if (pointerId === null || records.every(({ type }) => type !== 'childList' && type !== 'attributes')) {
      return;
    }
    dirty = true;
    if (active) {
      startAnimation();
    } else {
      refreshItems();
    }
  });
  mutationObserver.observe(node, { attributes: true, attributeFilter: ['style'], childList: true, subtree: true });

  node.addEventListener('pointerdown', onPointerDown);
  node.addEventListener('lostpointercapture', onLostPointerCapture);
  document.addEventListener('pointermove', onPointerMove, { capture: true });
  document.addEventListener('pointerup', onPointerUp, { capture: true });
  document.addEventListener('pointercancel', onPointerCancel, { capture: true });
  document.addEventListener('click', onClickCapture, { capture: true });
  document.addEventListener('keydown', onKeyDown, { capture: true });
  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('blur', onBlur);
  currentOptions.onControllerReady?.(cancelGesture);

  return {
    update(next: RectangleSelectionOptions<T>) {
      currentOptions = next;
    },
    destroy() {
      cancelGesture();
      if (suppressionTimer) {
        clearTimeout(suppressionTimer);
      }
      resizeObserver.disconnect();
      mutationObserver.disconnect();
      node.removeEventListener('pointerdown', onPointerDown);
      node.removeEventListener('lostpointercapture', onLostPointerCapture);
      document.removeEventListener('pointermove', onPointerMove, { capture: true });
      document.removeEventListener('pointerup', onPointerUp, { capture: true });
      document.removeEventListener('pointercancel', onPointerCancel, { capture: true });
      document.removeEventListener('click', onClickCapture, { capture: true });
      document.removeEventListener('keydown', onKeyDown, { capture: true });
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('blur', onBlur);
      currentOptions.onControllerReady?.(() => {});
    },
  };
}
