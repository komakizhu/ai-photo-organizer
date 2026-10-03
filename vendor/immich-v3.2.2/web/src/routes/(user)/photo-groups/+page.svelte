<script lang="ts">
  import { onMount } from 'svelte';
  import UserPageLayout from '$lib/components/layouts/UserPageLayout.svelte';
  import PhotoStudioNavigation from '$lib/components/shared-components/PhotoStudioNavigation.svelte';
  import type { PageData } from './$types';

  type Item = {
    relativePath: string;
    originalName: string;
    virtualGroup: string;
    sortTime: { value: string | null; source: string | null };
    vision: { category: string; needsReview: boolean; tags: string[]; reason: string; ocrText: string[] } | null;
    models: {
      qwen?: { short?: string | null; long?: string | null };
      afs?: { translatedShort?: string | null; extended?: string | null };
      codex?: { short?: string | null; long?: string | null };
    };
  };
  type Batch = { batchId: string; items: Item[] };
  type VisionJob = {
    running: boolean;
    status: string;
    completed: number;
    total: number;
    errors: { file?: string; relativePath?: string; error: string }[];
  };
  type FacePhoto = {
    id: string;
    name: string;
    relativePath: string;
    status: 'both' | 'disagreement' | 'neither';
    immich: unknown[];
    vision: unknown[];
  };
  type Pair = { key: string; aId: string; bId: string; distance: number };
  type Preset = { id: string; name: string; assetIds: string[] };
  type Review = {
    available: boolean;
    reason?: string;
    photos?: FacePhoto[];
    pairs?: Pair[];
    presets?: Preset[];
    confirmedGroups?: { assetIds: string[]; conflict: boolean }[];
    reviewQueue?: Pair[];
    decisions?: Record<string, { value: string }>;
  };

  let { data }: { data: PageData } = $props();
  const bridge = import.meta.env.VITE_PHOTO_STUDIO_BRIDGE || 'http://127.0.0.1:8766';
  let batch = $state<Batch | null>(null);
  let review = $state<Review | null>(null);
  let token = $state('');
  let vision = $state<VisionJob | null>(null);
  let activeGroup = $state('全部');
  let searchText = $state('');
  let selected = $state<Record<string, boolean>>({});
  let newPersonName = $state('');
  let targetPreset = $state('');
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');

  const categories = $derived(
    (batch?.items ?? []).reduce<Record<string, number>>((counts, item) => {
      counts[item.virtualGroup] = (counts[item.virtualGroup] ?? 0) + 1;
      return counts;
    }, {}),
  );
  const peopleByPath = $derived(Object.fromEntries((review?.photos ?? []).map((photo) => [photo.relativePath, photo])));
  const peopleById = $derived(Object.fromEntries((review?.photos ?? []).map((photo) => [photo.id, photo])));
  const selectedIds = $derived(
    Object.entries(selected)
      .filter(([, value]) => value)
      .map(([id]) => id),
  );
  const visibleItems = $derived(
    (batch?.items ?? [])
      .filter((item) => {
        if (activeGroup.startsWith('person:')) {
          const preset = review?.presets?.find((entry) => entry.id === activeGroup.slice(7));
          if (!preset?.assetIds.includes(peopleByPath[item.relativePath]?.id)) return false;
        } else if (activeGroup !== '全部' && item.virtualGroup !== activeGroup) {
          return false;
        }
        const query = searchText.trim().toLocaleLowerCase();
        if (!query) return true;
        return [
          item.originalName,
          item.relativePath,
          item.virtualGroup,
          item.models.qwen?.short,
          item.models.qwen?.long,
          item.models.afs?.translatedShort,
          item.models.afs?.extended,
          item.models.codex?.short,
          item.models.codex?.long,
          ...(item.vision?.ocrText ?? []),
        ].some((value) => value?.toLocaleLowerCase().includes(query));
      })
      .sort((a, b) => (b.sortTime.value ?? '').localeCompare(a.sortTime.value ?? '')),
  );

  const thumbnail = (path: string) => `${bridge}/v1/thumbnail/${encodeURIComponent(path)}`;
  const faceStatus = (photo?: FacePhoto) =>
    photo && review?.decisions?.[`photo:${photo.id}`]?.value === 'has-face'
      ? '你已确认有人（检测可能漏掉）'
      : photo && review?.decisions?.[`photo:${photo.id}`]?.value === 'no-face'
        ? '你已确认无人'
        : photo?.status === 'both'
          ? '两边同处检出脸'
          : photo?.status === 'disagreement'
            ? '两边检测有分歧'
            : photo?.status === 'neither'
              ? '两边都没检出脸'
              : '尚无人脸审核';
  const toggleSelected = (id: string) => {
    selected[id] = !selected[id];
  };

  const refresh = async () => {
    try {
      const response = await fetch(`${bridge}/v1/batch`, { cache: 'no-store' });
      if (!response.ok) throw new Error('本地整理服务未连接');
      const result = await response.json();
      batch = result.batch as Batch;
      token = result.token;
      vision = result.vision as VisionJob;
      const faceResponse = await fetch(`${bridge}/v1/people-review`, { cache: 'no-store' });
      if (!faceResponse.ok) throw new Error('人物审核记录暂不可用');
      review = (await faceResponse.json()) as Review;
      error = '';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    }
  };

  const startVisionScan = async () => {
    if (!batch || busy || vision?.running) return;
    busy = true;
    error = '';
    try {
      const response = await fetch(`${bridge}/v1/vision/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? 'Apple Vision 扫描启动失败');
      vision = result.vision as VisionJob;
      notice = vision.running
        ? 'Vision 扫描已开始。'
        : '扫描结果已是最新。';
      await refresh();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const postReview = async (action: Record<string, unknown>) => {
    if (!batch || busy) return;
    busy = true;
    try {
      const response = await fetch(`${bridge}/v1/people-review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ ...action, batchId: batch.batchId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? '保存失败');
      selected = {};
      await refresh();
      notice = '已保存人物组。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const createPreset = () => {
    if (!newPersonName.trim() || !selectedIds.length) return;
    void postReview({ action: 'create', name: newPersonName.trim(), assetIds: selectedIds });
    newPersonName = '';
  };
  const updatePreset = (action: 'add' | 'remove') => {
    if (!targetPreset || !selectedIds.length) return;
    void postReview({ action, presetId: targetPreset, assetIds: selectedIds });
  };
  const createFromGroup = (index: number, ids: string[]) => {
    const input = document.getElementById(`person-group-${index}`) as HTMLInputElement | null;
    const name = input?.value.trim();
    if (name) void postReview({ action: 'create', name, assetIds: ids });
  };

  onMount(() => {
    void refresh();
    const timer = setInterval(() => {
      if (vision?.running) void refresh();
    }, 2500);
    return () => clearInterval(timer);
  });
</script>

<UserPageLayout title={data.meta.title} scrollbar={true}>
  <div class="mx-auto max-w-7xl space-y-6 p-5 pb-24">
    <PhotoStudioNavigation active="groups" />
    <div class="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <h2 class="text-xl font-semibold">智能分组</h2>
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <button
          class="rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white disabled:opacity-40"
          onclick={startVisionScan}
          disabled={!batch || busy || vision?.running}
          >{vision?.running ? `Apple Vision 扫描中 ${vision.completed}/${vision.total}` : '扫描当前照片'}</button
        >
        <span class="text-sm text-gray-600 dark:text-gray-300">
          {vision?.status === 'complete'
            ? `扫描完成 ${vision.completed}/${vision.total}`
            : vision?.status === 'partial' || vision?.status === 'failed' || vision?.status === 'interrupted'
              ? `未完成 ${vision.completed}/${vision.total}；重试会继续。`
              : '尚未扫描'}
        </span>
      </div>
      {#if vision?.running}
        <progress class="mt-3 h-2 w-full accent-blue-600" value={vision.completed} max={Math.max(vision.total, 1)}
        ></progress>
      {/if}
      {#if vision?.errors.length}
        <details class="mt-3 text-sm text-amber-800 dark:text-amber-300">
          <summary class="cursor-pointer">{vision.errors.length} 个失败或待重试项</summary>
          <div class="mt-2 space-y-1">
            {#each vision.errors.slice(0, 20) as failure}
              <p class="break-all">{failure.relativePath ?? failure.file ?? '任务'}：{failure.error}</p>
            {/each}
          </div>
        </details>
      {/if}
      {#if error}<p class="mt-3 text-red-600" role="alert">{error}</p>{/if}
      {#if notice}<p class="mt-3 text-green-700" role="status">{notice}</p>{/if}
      <label class="mt-4 block text-sm"
        >搜索照片
        <input
          class="mt-1 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2"
          placeholder="文件名、画面描述或图片文字"
          bind:value={searchText}
        />
      </label>
    </div>

    <nav class="flex flex-wrap gap-2" aria-label="照片虚拟分组">
      <button
        class:font-semibold={activeGroup === '全部'}
        class="rounded-lg border px-3 py-2"
        onclick={() => (activeGroup = '全部')}>全部 {batch?.items.length ?? 0}</button
      >
      {#each Object.entries(categories) as [group, count]}
        <button
          class:font-semibold={activeGroup === group}
          class="rounded-lg border px-3 py-2"
          onclick={() => (activeGroup = group)}>{group} {count}</button
        >
      {/each}
    </nav>

    <section
      class="space-y-3 rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900"
      aria-label="人物分组"
    >
      <h3 class="text-lg font-semibold">人物分组</h3>
      {#if review?.available}
        <p class="text-sm text-gray-600 dark:text-gray-300">
          相似人脸只是候选，请人工确认。
        </p>
        <div class="flex flex-wrap gap-2">
          {#each review.presets ?? [] as preset}
            <button
              class:font-semibold={activeGroup === `person:${preset.id}`}
              class="rounded-lg border px-3 py-2"
              onclick={() => (activeGroup = `person:${preset.id}`)}>{preset.name} · {preset.assetIds.length}</button
            >
          {/each}
        </div>
        {#if (review.confirmedGroups ?? []).length}
          <div class="grid gap-3 md:grid-cols-2">
            {#each review.confirmedGroups ?? [] as group, index}
              {@const savedAs = (review.presets ?? []).find((preset) =>
                group.assetIds.every((id) => preset.assetIds.includes(id)),
              )}
              <div class="rounded-xl border p-3">
                <p class="font-medium">人工判断为同一人 · {group.assetIds.length} 张</p>
                {#if group.conflict}<p class="text-amber-700">判断冲突，请复核。</p>
                {:else if savedAs}<p class="mt-2 text-sm text-green-700">已保存为 {savedAs.name}</p>
                {:else}
                  <div class="mt-2 flex gap-2">
                    <input
                      id={`person-group-${index}`}
                      class="min-w-0 flex-1 rounded-lg border px-3 py-2"
                      placeholder="输入这个人的名字"
                      maxlength="40"
                    />
                    <button
                      class="rounded-lg border px-3 py-2 disabled:opacity-40"
                      disabled={busy}
                      onclick={() => createFromGroup(index, group.assetIds)}>保存为人物组</button
                    >
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
        <div class="flex flex-wrap items-center gap-2 rounded-xl bg-gray-50 p-3 dark:bg-gray-800">
          <span>已选 {selectedIds.length} 张</span>
          <input
            class="rounded-lg border px-3 py-2"
            placeholder="新人物名称"
            maxlength="40"
            bind:value={newPersonName}
          />
          <button
            class="rounded-lg border px-3 py-2 disabled:opacity-40"
            disabled={busy || !selectedIds.length || !newPersonName.trim()}
            onclick={createPreset}>新建人物组</button
          >
          <select class="rounded-lg border bg-transparent px-3 py-2" bind:value={targetPreset} aria-label="选择人物组">
            <option value="">选择现有人物组</option>
            {#each review.presets ?? [] as preset}<option value={preset.id}>{preset.name}</option>{/each}
          </select>
          <button
            class="rounded-lg border px-3 py-2 disabled:opacity-40"
            disabled={busy || !selectedIds.length || !targetPreset}
            onclick={() => updatePreset('add')}>加入</button
          >
          <button
            class="rounded-lg border px-3 py-2 disabled:opacity-40"
            disabled={busy || !selectedIds.length || !targetPreset}
            onclick={() => updatePreset('remove')}>移出</button
          >
          <button class="rounded-lg border px-3 py-2" onclick={() => (selected = {})}>清空选择</button>
        </div>
        {#if (review.reviewQueue ?? []).length}
          <h4 class="font-semibold">还需你判断的相似照片</h4>
          <div class="grid gap-3 md:grid-cols-2">
            {#each review.reviewQueue ?? [] as pair}
              <div class="rounded-xl border p-3">
                <div class="grid grid-cols-2 gap-2">
                  {#each [pair.aId, pair.bId] as id}
                    {#if peopleById[id]}
                      <div>
                        <img
                          class="h-40 w-full rounded-lg bg-gray-100 object-contain"
                          src={thumbnail(peopleById[id].relativePath)}
                          alt={peopleById[id].name}
                          loading="lazy"
                        />
                        <p class="break-all text-xs">{peopleById[id].name}</p>
                      </div>
                    {/if}
                  {/each}
                </div>
                <div class="mt-2 flex gap-2">
                  <button
                    class="rounded-lg border px-3 py-2 disabled:opacity-40"
                    disabled={busy}
                    onclick={() => postReview({ action: 'decision', kind: 'pair', key: pair.key, value: 'same' })}
                    >同一人</button
                  >
                  <button
                    class="rounded-lg border px-3 py-2 disabled:opacity-40"
                    disabled={busy}
                    onclick={() => postReview({ action: 'decision', kind: 'pair', key: pair.key, value: 'different' })}
                    >不是同一人</button
                  >
                </div>
              </div>
            {/each}
          </div>
        {/if}
        {#if (review.pairs ?? []).some((pair) => review?.decisions?.[`pair:${pair.key}`])}
          <details class="rounded-xl border p-3">
            <summary class="cursor-pointer font-medium">查看或修改以前的人物判断</summary>
            <div class="mt-3 grid gap-3 md:grid-cols-2">
              {#each (review.pairs ?? []).filter((pair) => review?.decisions?.[`pair:${pair.key}`]) as pair}
                <div class="rounded-xl border p-3">
                  <div class="grid grid-cols-2 gap-2">
                    {#each [pair.aId, pair.bId] as id}
                      {#if peopleById[id]}
                        <div>
                          <img
                            class="h-32 w-full rounded-lg bg-gray-100 object-contain"
                            src={thumbnail(peopleById[id].relativePath)}
                            alt={peopleById[id].name}
                            loading="lazy"
                          />
                          <p class="break-all text-xs">{peopleById[id].name}</p>
                        </div>
                      {/if}
                    {/each}
                  </div>
                  <p class="mt-2 text-sm">
                    当前判断：{review.decisions?.[`pair:${pair.key}`]?.value === 'same'
                      ? '同一人'
                      : review.decisions?.[`pair:${pair.key}`]?.value === 'different'
                        ? '不是同一人'
                        : '稍后'}
                  </p>
                  <div class="mt-2 flex gap-2">
                    <button
                      class="rounded-lg border px-3 py-2 disabled:opacity-40"
                      disabled={busy}
                      onclick={() => postReview({ action: 'decision', kind: 'pair', key: pair.key, value: 'same' })}
                      >改为同一人</button
                    >
                    <button
                      class="rounded-lg border px-3 py-2 disabled:opacity-40"
                      disabled={busy}
                      onclick={() =>
                        postReview({ action: 'decision', kind: 'pair', key: pair.key, value: 'different' })}
                      >改为不同人</button
                    >
                  </div>
                </div>
              {/each}
            </div>
          </details>
        {/if}
      {:else}
        <p class="text-sm text-gray-600 dark:text-gray-300">{review?.reason ?? '正在读取本地人物审核记录…'}</p>
      {/if}
    </section>

    <section class="grid gap-4" aria-label="分组照片">
      {#each visibleItems as item (item.relativePath)}
        {@const face = peopleByPath[item.relativePath]}
        <article
          class="grid gap-4 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm md:grid-cols-[256px_1fr] dark:border-gray-700 dark:bg-gray-900"
        >
          {#if face}
            <button
              type="button"
              class={`flex h-64 w-full items-center justify-center overflow-hidden rounded-xl bg-gray-100 ${selected[face.id] ? 'ring-4 ring-blue-500' : ''}`}
              aria-pressed={Boolean(selected[face.id])}
              aria-label={`${selected[face.id] ? '取消选择' : '选择'} ${item.originalName}`}
              onclick={() => toggleSelected(face.id)}
              ><img
                class="h-full w-full object-contain"
                src={thumbnail(item.relativePath)}
                alt=""
                loading="lazy"
              /></button
            >
          {:else}
            {#if /\.(jpe?g|png|heic|tiff?|webp)$/i.test(item.originalName)}
              <img
                class="h-64 w-full rounded-xl bg-gray-100 object-contain"
                src={thumbnail(item.relativePath)}
                alt={item.originalName}
                loading="lazy"
              />
            {:else}
              <div class="flex h-64 w-full items-center justify-center rounded-xl bg-gray-100 text-sm text-gray-600">
                视频无预览
              </div>
            {/if}
          {/if}
          <div class="min-w-0 space-y-2">
            <p class="font-semibold">{item.virtualGroup}</p>
            {#if item.vision}
              <p class="text-sm text-gray-600 dark:text-gray-300">
                Vision 建议：{item.vision.category} · {item.vision.reason}{item.vision.needsReview
                  ? ' · 待人工确认'
                  : ''}
              </p>
            {/if}
            <p class="break-all text-sm">{item.originalName}</p>
            {#if item.sortTime.value}<p class="text-sm text-gray-600 dark:text-gray-300">
                {item.sortTime.source === 'capture' ? '拍摄' : '文件时间'}：{item.sortTime.value}
              </p>{/if}
            <p class="text-sm text-gray-600 dark:text-gray-300">{faceStatus(face)}</p>
            {#if face}
              <div class="flex flex-wrap gap-2 text-sm">
                {#each (review?.presets ?? []).filter((preset) => preset.assetIds.includes(face.id)) as preset}
                  <span class="rounded-full bg-blue-50 px-3 py-1 text-blue-800">{preset.name}</span>
                {/each}
              </div>
              {#if face.status !== 'both'}
                <div class="flex gap-2">
                  <button
                    class="rounded-lg border px-3 py-2 disabled:opacity-40"
                    disabled={busy}
                    onclick={() => postReview({ action: 'decision', kind: 'photo', key: face.id, value: 'has-face' })}
                    >这张有人</button
                  >
                  <button
                    class="rounded-lg border px-3 py-2 disabled:opacity-40"
                    disabled={busy}
                    onclick={() => postReview({ action: 'decision', kind: 'photo', key: face.id, value: 'no-face' })}
                    >确实无人</button
                  >
                </div>
              {/if}
            {/if}
            {#if item.models.qwen?.long}<p class="text-sm">{item.models.qwen.long}</p>{/if}
            {#if item.models.afs?.extended}<p class="text-sm text-gray-600 dark:text-gray-300">
                {item.models.afs.extended}
              </p>{/if}
            {#if item.models.codex?.long}<p class="text-sm text-gray-600 dark:text-gray-300">
                {item.models.codex.long}
              </p>{/if}
          </div>
        </article>
      {/each}
    </section>
  </div>
</UserPageLayout>
