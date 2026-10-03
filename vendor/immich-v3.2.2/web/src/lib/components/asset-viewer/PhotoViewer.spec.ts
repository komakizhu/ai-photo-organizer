import { fireEvent, render } from '@testing-library/svelte';
import { assetViewerManager } from '$lib/managers/asset-viewer-manager.svelte';
import { SlideshowState, slideshowStore } from '$lib/stores/slideshow.store';
import { assetFactory } from '@test-data/factories/asset-factory';
import PhotoViewer from './PhotoViewer.svelte';

describe('PhotoViewer background click', () => {
  afterEach(() => {
    assetViewerManager.resetZoomState();
    assetViewerManager.closeEditor();
    assetViewerManager.closeFaceEditMode();
    slideshowStore.slideshowState.set(SlideshowState.None);
  });

  it('closes from empty backdrop space but not from the image itself', async () => {
    const onClose = vi.fn();
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const { container } = render(PhotoViewer, { cursor: { current: asset }, onClose });

    const photoViewer = container.firstElementChild as HTMLElement;
    const backdrop = photoViewer.querySelector('.relative.size-full.overflow-hidden') as HTMLElement;
    expect(backdrop).not.toBeNull();

    await fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledOnce();

    await fireEvent.click(photoViewer);
    expect(onClose).toHaveBeenCalledTimes(2);

    const image = document.createElement('img');
    backdrop.append(image);
    await fireEvent.click(image);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('does not close while slideshow or photo editing is active', async () => {
    const onClose = vi.fn();
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const { container } = render(PhotoViewer, { cursor: { current: asset }, onClose });
    const backdrop = container.querySelector('.relative.size-full.overflow-hidden') as HTMLElement;

    slideshowStore.slideshowState.set(SlideshowState.PlaySlideshow);
    await fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();

    slideshowStore.slideshowState.set(SlideshowState.None);
    assetViewerManager.toggleFaceEditMode();
    await fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();

    assetViewerManager.closeFaceEditMode();
    assetViewerManager.openEditor();
    await fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps the photo open when its pointer-transparent image area is clicked', async () => {
    const onClose = vi.fn();
    const asset = assetFactory.build({ originalPath: 'image.jpg', originalMimeType: 'image/jpeg' });
    const { container } = render(PhotoViewer, { cursor: { current: asset }, onClose });
    const backdrop = container.querySelector('.relative.size-full.overflow-hidden') as HTMLElement;
    const bounds = container.querySelector('[data-photo-bounds]') as HTMLElement;
    vi.spyOn(bounds, 'getBoundingClientRect').mockReturnValue({
      left: 100,
      top: 100,
      right: 300,
      bottom: 300,
      width: 200,
      height: 200,
      x: 100,
      y: 100,
      toJSON: () => ({}),
    });
    await fireEvent.click(backdrop, { clientX: 150, clientY: 150 });
    expect(onClose).not.toHaveBeenCalled();
    await fireEvent.click(backdrop, { clientX: 50, clientY: 150 });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
