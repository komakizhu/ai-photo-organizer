<script lang="ts">
  import { page } from '$app/state';
  import { isPhotoStudioRoute } from '$lib/utils/photo-studio';
  import UploadCover from './DragAndDropUploadOverlay.svelte';
  import { assetViewerManager } from '$lib/managers/asset-viewer-manager.svelte';
  import { isAssetViewerVisible } from '$lib/utils/navigation';
  import type { Snippet } from 'svelte';
  interface Props {
    children?: Snippet;
  }

  let { children }: Props = $props();
  const viewerVisible = $derived(isAssetViewerVisible(page, assetViewerManager.isViewing));

  // $page.data.asset is loaded by route specific +page.ts loaders if that
  // route contains the assetId path.
  $effect.pre(() => {
    if (page.data.asset && isAssetViewerVisible(page, true)) {
      assetViewerManager.setAsset(page.data.asset);
    } else {
      assetViewerManager.showAssetViewer(false);
    }
    const asset = page.url.searchParams.get('at');
    assetViewerManager.gridScrollTarget = { at: asset };
  });
</script>

<div inert={viewerVisible} aria-hidden={viewerVisible || undefined}>
  {@render children?.()}
</div>
{#if !isPhotoStudioRoute(page.url.pathname)}
  <UploadCover />
{/if}

<style>
  :root {
    overscroll-behavior: none;
  }
</style>
