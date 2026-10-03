import { fireEvent, render } from '@testing-library/svelte';
import { getIntersectionObserverMock } from '$lib/__mocks__/intersection-observer.mock';
import Thumbnail from '$lib/components/assets/thumbnail/Thumbnail.svelte';
import { getTabbable } from '$lib/utils/focus-util';
import { assetFactory } from '@test-data/factories/asset-factory';

vi.mock('$lib/utils/navigation', () => ({
  currentUrlReplaceAssetId: vi.fn(),
  isSharedLinkRoute: vi.fn().mockReturnValue(false),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, 'matchMedia', {
    writable: true,
    enumerable: true,
    value: vi.fn().mockImplementation(function (query) {
      return {
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(), // deprecated
        removeListener: vi.fn(), // deprecated
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      };
    }),
  });
});

describe('Thumbnail component', () => {
  beforeAll(() => {
    vi.stubGlobal('IntersectionObserver', getIntersectionObserverMock());
  });

  it('should only contain a single tabbable element (the container)', () => {
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const { baseElement } = render(Thumbnail, {
      asset,
      selected: true,
    });

    const container = baseElement.querySelector('[data-thumbnail-focus-container]');
    expect(container).not.toBeNull();
    expect(container!.getAttribute('tabindex')).toBe('0');

    // Guarding against inserting extra tabbable elements in future in <Thumbnail/>
    const tabbables = getTabbable(container!);
    expect(tabbables.length).toBe(0);
  });

  it('shows thumbhash while image is loading', () => {
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const sut = render(Thumbnail, {
      asset,
      selected: true,
    });

    const thumbhash = sut.getByTestId('thumbhash');
    expect(thumbhash).not.toBeFalsy();
  });

  it('uses the image surface for selection and keeps zoom independent', async () => {
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const onClick = vi.fn();
    const onSelect = vi.fn();
    const onPreview = vi.fn();
    const { baseElement, getByRole } = render(Thumbnail, {
      asset,
      selectionInteraction: 'direct',
      onClick,
      onSelect,
      onPreview,
    });

    const thumbnail = baseElement.querySelector('[data-thumbnail-focus-container]')!;
    await fireEvent.click(thumbnail, { ctrlKey: true, metaKey: true });
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onClick).not.toHaveBeenCalled();

    const zoom = getByRole('button', { name: 'Zoom image' });
    expect(zoom.className).toContain('size-9');
    await fireEvent.click(zoom);
    expect(onPreview).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledOnce();
  });

  it('supports Enter and Space selection in direct mode', async () => {
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const onSelect = vi.fn();
    const { baseElement } = render(Thumbnail, { asset, selectionInteraction: 'direct', onSelect });
    const thumbnail = baseElement.querySelector('[data-thumbnail-focus-container]')!;

    await fireEvent.keyDown(thumbnail, { key: 'Enter' });
    await fireEvent.keyDown(thumbnail, { key: ' ' });
    expect(onSelect).toHaveBeenCalledTimes(2);
  });
});
