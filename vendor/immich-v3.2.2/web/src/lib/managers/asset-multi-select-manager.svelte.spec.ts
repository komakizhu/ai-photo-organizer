import { AssetVisibility } from '@immich/sdk';
import { AssetMultiSelectManager } from '$lib/managers/asset-multi-select-manager.svelte';
import { authManager } from '$lib/managers/auth-manager.svelte';
import { timelineAssetFactory } from '@test-data/factories/asset-factory';
import { preferencesFactory } from '@test-data/factories/preferences-factory';
import { userAdminFactory } from '@test-data/factories/user-factory';

describe('AssetMultiSelectManager', () => {
  let sut: AssetMultiSelectManager;

  beforeEach(() => {
    sut = new AssetMultiSelectManager();
  });

  it('calculates derived values from selection', () => {
    sut.selectAsset(
      timelineAssetFactory.build({ isFavorite: true, visibility: AssetVisibility.Archive, isTrashed: true }),
    );
    sut.selectAsset(
      timelineAssetFactory.build({ isFavorite: true, visibility: AssetVisibility.Timeline, isTrashed: false }),
    );

    expect(sut.selectionActive).toBe(true);
    expect(sut.isAllTrashed).toBe(false);
    expect(sut.isAllArchived).toBe(false);
    expect(sut.isAllFavorite).toBe(true);
  });

  it('replaces a rectangle selection together with its group and Shift anchor', () => {
    const previous = timelineAssetFactory.build();
    const first = timelineAssetFactory.build();
    const second = timelineAssetFactory.build();
    sut.selectAsset(previous);

    sut.replaceSelection([first, second], ['2026-10-02'], first);

    expect(sut.assets.map(({ id }) => id)).toEqual([first.id, second.id]);
    expect(sut.selectedGroup.has('2026-10-02')).toBe(true);
    expect(sut.startAsset?.id).toBe(first.id);
    expect(sut.candidates).toEqual([]);
    expect(sut.selectAll).toBe(false);
  });

  it('updates isAllUserOwned when the active user changes', () => {
    const [user1, user2] = userAdminFactory.buildList(2);
    sut.selectAsset(timelineAssetFactory.build({ ownerId: user1.id }));

    const cleanup = $effect.root(() => {
      expect(sut.isAllUserOwned).toBe(false);

      authManager.setUser(user1);
      authManager.setPreferences(preferencesFactory.build());
      expect(sut.isAllUserOwned).toBe(true);

      authManager.setUser(user2);
      expect(sut.isAllUserOwned).toBe(false);
    });

    cleanup();
    authManager.reset();
  });
});
