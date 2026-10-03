import { AssetTypeEnum, updateAsset } from '@immich/sdk';
import { fireEvent, waitFor } from '@testing-library/svelte';
import { getAnimateMock } from '$lib/__mocks__/animate.mock';
import { getResizeObserverMock } from '$lib/__mocks__/resize-observer.mock';
import { assetViewerManager } from '$lib/managers/asset-viewer-manager.svelte';
import { authManager } from '$lib/managers/auth-manager.svelte';
import { SlideshowState, slideshowStore } from '$lib/stores/slideshow.store';
import ViewerPortalFixture from '$tests/ViewerPortalFixture.svelte';
import { renderWithTooltips } from '$tests/helpers';
import { assetFactory } from '@test-data/factories/asset-factory';
import { preferencesFactory } from '@test-data/factories/preferences-factory';
import { userAdminFactory } from '@test-data/factories/user-factory';
import AssetViewer from './AssetViewer.svelte';

vi.mock('$lib/managers/feature-flags-manager.svelte', () => ({
  featureFlagsManager: {
    init: vi.fn(),
    loadFeatureFlags: vi.fn(),
    value: { smartSearch: true, trash: true },
  } as never,
}));

vi.mock('$lib/stores/ocr.svelte', () => ({
  ocrManager: {
    clear: vi.fn(),
    getAssetOcr: vi.fn(),
    hasOcrData: false,
    showOverlay: false,
  },
}));

vi.mock('@immich/sdk', async () => {
  const sdk = await vi.importActual<typeof import('@immich/sdk')>('@immich/sdk');
  return {
    ...sdk,
    getFaces: vi.fn().mockResolvedValue([]),
    updateAsset: vi.fn(),
  };
});

describe('AssetViewer', () => {
  beforeAll(() => {
    Element.prototype.animate = getAnimateMock();
    vi.stubGlobal('ResizeObserver', getResizeObserverMock());
  });

  afterEach(() => {
    slideshowStore.slideshowState.set(SlideshowState.None);
    assetViewerManager.showAssetViewer(false);
    authManager.reset();
    vi.clearAllMocks();
  });

  afterAll(() => {
    vi.restoreAllMocks();
  });

  it('closes the photo viewer when the empty background is clicked', async () => {
    const ownerId = 'owner-id';
    const user = userAdminFactory.build({ id: ownerId });
    const asset = assetFactory.build({ ownerId, type: AssetTypeEnum.Image });
    const onClose = vi.fn();

    authManager.setUser(user);
    authManager.setPreferences(preferencesFactory.build({ cast: { gCastEnabled: false } }));
    assetViewerManager.showAssetViewer(true);

    const { container } = renderWithTooltips(AssetViewer, {
      cursor: { current: asset },
      showNavigation: false,
      onClose,
    });

    const viewerBackground = container.querySelector('[data-viewer-content]');
    expect(viewerBackground).not.toBeNull();

    await fireEvent.click(viewerBackground!);

    expect(onClose).toHaveBeenCalledWith(asset.id);
    expect(assetViewerManager.isViewing).toBe(false);
    // Navigation callbacks can be asynchronous: closing must not depend on the parent unmounting us.
    expect(container.querySelector('#immich-asset-viewer')).not.toBeVisible();
    await fireEvent.click(viewerBackground!);
    expect(onClose).toHaveBeenCalledOnce();
    assetViewerManager.showAssetViewer(true);
    await waitFor(() => expect(container.querySelector('#immich-asset-viewer')).toBeVisible());
  });

  it.each(['toolbar', 'previous', 'next'])('closes from empty %s overlay margins', async (area) => {
    const asset = assetFactory.build({ ownerId: 'owner-id', type: AssetTypeEnum.Image });
    authManager.setUser(userAdminFactory.build({ id: asset.ownerId }));
    authManager.setPreferences(preferencesFactory.build({ cast: { gCastEnabled: false } }));
    assetViewerManager.showAssetViewer(true);
    const onClose = vi.fn();
    const { container } = renderWithTooltips(AssetViewer, {
      cursor: { current: asset, previousAsset: assetFactory.build(), nextAsset: assetFactory.build() },
      showNavigation: true,
      onClose,
    });
    const overlays = container.querySelectorAll('[data-viewer-background]');
    const overlay = overlays[{ toolbar: 0, previous: 1, next: 2 }[area]!];
    expect(overlay).toBeDefined();
    const bounds = container.querySelector('[data-photo-bounds]')!;
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
    // An overlay over the photo is not empty; nor is an interactive descendant.
    await fireEvent.click(overlay, { clientX: 150, clientY: 150 });
    const control = document.createElement('button');
    overlay.append(control);
    await fireEvent.click(control, { clientX: 50, clientY: 50 });
    expect(onClose).not.toHaveBeenCalled();
    await fireEvent.click(overlay, { clientX: 50, clientY: 50 });
    expect(onClose).toHaveBeenCalledOnce();
    expect(container.querySelector('#immich-asset-viewer')).not.toBeVisible();
  });

  it.each(['background', 'return'] as const)('removes the portalled viewer on %s close', async (action) => {
    const ownerId = 'owner-id';
    const asset = assetFactory.build({ ownerId, type: AssetTypeEnum.Image });
    const onClose = vi.fn();
    authManager.setUser(userAdminFactory.build({ id: ownerId }));
    authManager.setPreferences(preferencesFactory.build({ cast: { gCastEnabled: false } }));
    assetViewerManager.showAssetViewer(true);
    const { getByRole } = renderWithTooltips(ViewerPortalFixture, { asset, onClose });
    await waitFor(() => expect(document.querySelector('#immich-asset-viewer')).not.toBeNull());
    const target =
      action === 'background'
        ? document.querySelector('[data-viewer-content]')!
        : getByRole('button', { name: 'go_back' });
    await fireEvent.click(target);
    await waitFor(() => expect(document.querySelector('#immich-asset-viewer')).toBeNull());
    expect(onClose).toHaveBeenCalledWith(asset.id);
  });

  it('does not leave a viewer over the gallery route when the manager still has an open asset', async () => {
    const ownerId = 'owner-id';
    const asset = assetFactory.build({ ownerId, type: AssetTypeEnum.Image });
    authManager.setUser(userAdminFactory.build({ id: ownerId }));
    authManager.setPreferences(preferencesFactory.build({ cast: { gCastEnabled: false } }));
    assetViewerManager.setAsset(asset);
    const onClose = vi.fn();
    const { rerender, getByText } = renderWithTooltips(ViewerPortalFixture, { asset, onClose });
    await waitFor(() => expect(document.querySelector('#immich-asset-viewer')).not.toBeNull());
    await rerender({
      component: ViewerPortalFixture,
      componentProps: { asset, onClose, galleryRoute: true },
    } as never);
    await waitFor(() => expect(document.querySelector('#immich-asset-viewer')).toBeNull());
    expect(getByText('Gallery')).not.toHaveAttribute('inert');
  });

  it.skip('updates the top bar favorite action after pressing favorite', async () => {
    const ownerId = 'owner-id';
    const user = userAdminFactory.build({ id: ownerId });
    const asset = assetFactory.build({ ownerId, isFavorite: false, isTrashed: false });

    authManager.setUser(user);
    authManager.setPreferences(preferencesFactory.build({ cast: { gCastEnabled: false } }));

    vi.mocked(updateAsset).mockResolvedValue({ ...asset, isFavorite: true });

    const { getByLabelText, queryByLabelText } = renderWithTooltips(AssetViewer, {
      cursor: { current: asset },
      showNavigation: false,
    });

    expect(getByLabelText('to_favorite')).toBeInTheDocument();
    expect(queryByLabelText('unfavorite')).toBeNull();

    await fireEvent.click(getByLabelText('to_favorite'));

    await waitFor(() =>
      expect(updateAsset).toHaveBeenCalledWith({ id: asset.id, updateAssetDto: { isFavorite: true } }),
    );
    await waitFor(() => expect(getByLabelText('unfavorite')).toBeInTheDocument());
  });
});
