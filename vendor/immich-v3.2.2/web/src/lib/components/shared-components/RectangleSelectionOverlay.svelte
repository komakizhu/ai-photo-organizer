<script lang="ts">
  import type { RectangleSelectionUpdate } from '$lib/utils/rectangle-selection';

  let { selection }: { selection: RectangleSelectionUpdate | null } = $props();
</script>

{#if selection}
  <div
    aria-hidden="true"
    class="pointer-events-none fixed z-50 border border-immich-primary bg-immich-primary/20 shadow-sm"
    style:left={selection.rectangle.left + 'px'}
    style:top={selection.rectangle.top + 'px'}
    style:width={Math.max(1, selection.rectangle.right - selection.rectangle.left) + 'px'}
    style:height={Math.max(1, selection.rectangle.bottom - selection.rectangle.top) + 'px'}
  ></div>
{/if}
