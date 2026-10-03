<script lang="ts">
  import AssetViewer from '$lib/components/asset-viewer/AssetViewer.svelte';
  import Portal from '$lib/elements/Portal.svelte';
  import { assetViewerManager } from '$lib/managers/asset-viewer-manager.svelte';
  import { isAssetViewerVisible } from '$lib/utils/navigation';
  import type { AssetResponseDto } from '@immich/sdk';

  let {
    asset,
    onClose,
    galleryRoute = false,
  }: { asset: AssetResponseDto; onClose: (id: string) => void; galleryRoute?: boolean } = $props();
  const viewerVisible = $derived(
    isAssetViewerVisible(
      {
        route: { id: '/(user)/photos/[[assetId=id]]' },
        params: galleryRoute ? {} : { assetId: asset.id },
      },
      assetViewerManager.isViewing,
    ),
  );
</script>

<div inert={viewerVisible}>Gallery</div>
<Portal target="body">
  {#if viewerVisible}
    <AssetViewer cursor={{ current: asset }} showNavigation={false} {onClose} />
  {/if}
</Portal>
