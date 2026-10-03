<script lang="ts">
  import { onMount } from 'svelte';
  import UserPageLayout from '$lib/components/layouts/UserPageLayout.svelte';
  import PhotoStudioNavigation from '$lib/components/shared-components/PhotoStudioNavigation.svelte';
  import type { PageData } from './$types';

  type Model = {
    model: string;
    short?: string | null;
    long?: string | null;
    extended?: string | null;
    translatedShort?: string | null;
    category?: string | null;
    people?: string | null;
    uncertainty?: string | null;
    note?: string | null;
    status?: string | null;
    details?: Record<string, unknown>;
  };
  type Item = {
    relativePath: string;
    assetId: string | null;
    versionId: string | null;
    originalName: string;
    folderHint: string;
    captureTime: { value: string | null; source: string | null; issue: string | null };
    fileTimes: { created: string | null; modified: string | null };
    sortTime: { value: string | null; source: 'capture' | 'file_time' | null };
    vision: {
      category: string;
      tags: string[];
      reason: string;
      needsReview: boolean;
      ocrText: string[];
    } | null;
    virtualGroup: string;
    personName?: string | null;
    suggestedName: string | null;
    contentOnlyName: string | null;
    issue: string | null;
    reason: string;
    category: string;
    humanCategory?: string | null;
    identityNeedsReview?: boolean;
    models: { qwen?: Model; afs?: Model; codex?: Model };
  };
  type Batch = { batchId: string; sourceId: string; source: string; createdAt: string; items: Item[] };
  type SourceCapabilities = { immichRename: boolean };
  type Analysis = {
    running: boolean;
    completed: number;
    total: number;
    status?: string;
    engines?: Record<string, { completed: number; total: number }>;
    errors: { file: string; engine?: string; error: string }[];
  };
  type Category = { category: string; enabled: boolean; description: string; examples: unknown[] };
  type DuplicateGroup = { review: unknown | null };
  type VisionStatus = {
    running: boolean;
    status: string;
    completed: number;
    total: number;
    errors: { file?: string; relativePath?: string; error: string }[];
  };
  type DuplicateStatus = Omit<Analysis, 'errors'> & {
    errors: { file?: string; relativePath?: string; error: string }[];
    exactGroups?: string[][];
    visualPairs?: unknown[];
  };
  type CodexPreview = {
    batchId: string;
    mode: 'all' | 'fallback';
    targets: { relativePath: string; reason: string }[];
  };
  type CodexStatus = Analysis & { mode: string | null };
  type Reconcile = {
    status: string;
    results: { old: string; new: string; status: string; changed?: Record<string, unknown> }[];
    error?: string;
  };

  let { data }: { data: PageData } = $props();
  const bridge = import.meta.env.VITE_PHOTO_STUDIO_BRIDGE || 'http://127.0.0.1:8766';
  let batch = $state<Batch | null>(null);
  let sourceCapabilities = $state<SourceCapabilities>({ immichRename: false });
  let token = $state('');
  let analysis = $state<Analysis | null>(null);
  let duplicates = $state<DuplicateStatus | null>(null);
  let vision = $state<VisionStatus | null>(null);
  let duplicateCounts = $state({ exact: 0, visual: 0, pending: 0 });
  let categories = $state<Category[]>([]);
  let categoryText = $state('');
  let categoryTextDirty = $state(false);
  let codex = $state<CodexStatus | null>(null);
  let codexMode = $state<'off' | 'all' | 'fallback'>('off');
  let codexPreview = $state<CodexPreview | null>(null);
  let codexConfirmation = $state('');
  let reconcile = $state<Reconcile | null>(null);
  let names = $state<Record<string, string>>({});
  let selected = $state<Record<string, boolean>>({});
  let busy = $state(false);
  let selectingSource = $state(false);
  let error = $state('');
  let notice = $state('');
  let confirmation = $state('');
  let renameEnabled = $state(false);
  const selectedCount = $derived(Object.values(selected).filter(Boolean).length);
  const sourceWorkRunning = $derived(
    busy ||
      analysis?.running ||
      duplicates?.running ||
      vision?.running ||
      codex?.running ||
      reconcile?.status === 'running',
  );
  const visibleItems = $derived(
    (batch?.items ?? []).slice().sort((a, b) => (b.sortTime.value ?? '').localeCompare(a.sortTime.value ?? '')),
  );
  const toggleSelected = (path: string) => {
    selected[path] = !selected[path];
  };
  const captureSource = (source: string | null) =>
    source?.startsWith('QuickTime')
      ? '视频内拍摄时间'
      : source?.startsWith('XMP')
        ? '图片内 XMP 拍摄时间'
        : '相机 EXIF 拍摄时间';
  const firstBatchTooLarge = $derived(reconcile?.status !== 'verified' && selectedCount > 2);
  const loadDuplicateSummary = async (expectedBatchId: string) => {
    const [exactResponse, visualResponse] = await Promise.all([
      fetch(`${bridge}/v1/duplicate-groups?kind=exact`, { cache: 'no-store' }),
      fetch(`${bridge}/v1/duplicate-groups?kind=visual`, { cache: 'no-store' }),
    ]);
    if (!exactResponse.ok || !visualResponse.ok) return false;
    const [exactResult, visualResult] = await Promise.all([exactResponse.json(), visualResponse.json()]);
    if (exactResult.batchId !== expectedBatchId || visualResult.batchId !== expectedBatchId) {
      return false;
    }
    const exact = (exactResult.groups ?? []) as DuplicateGroup[];
    const visual = (visualResult.groups ?? []) as DuplicateGroup[];
    duplicateCounts = {
      exact: exact.length,
      visual: visual.length,
      pending: [...exact, ...visual].filter((group) => !group.review).length,
    };
    return true;
  };
  const refresh = async (retrySourceChange = true) => {
    try {
      const response = await fetch(`${bridge}/v1/batch`, { cache: 'no-store' });
      if (!response.ok) throw new Error('本地协调服务未连接');
      const result = await response.json();
      const incoming = result.batch as Batch;
      if (batch?.batchId !== incoming.batchId) {
        names = Object.fromEntries(incoming.items.map((item) => [item.relativePath, item.suggestedName ?? '']));
        selected = {};
        renameEnabled = false;
        confirmation = '';
      }
      batch = incoming;
      sourceCapabilities = result.sourceCapabilities ?? { immichRename: false };
      token = result.token;
      analysis = result.analysis;
      duplicates = result.duplicates;
      vision = result.vision;
      codex = result.codex;
      if (codexPreview?.batchId !== incoming.batchId) codexPreview = null;
      reconcile = result.reconcile;
      const categoryResponse = await fetch(`${bridge}/v1/categories`, { cache: 'no-store' });
      if (categoryResponse.ok) {
        categories = (await categoryResponse.json()).categories as Category[];
        if (!categoryTextDirty) {
          categoryText = categories
            .filter((row) => row.enabled)
            .map((row) => row.category)
            .join('\n');
        }
      }
      const summaryMatches = await loadDuplicateSummary(incoming.batchId);
      if (!summaryMatches) {
        if (retrySourceChange) {
          await refresh(false);
          return;
        }
        batch = null;
        throw new Error('照片来源刚发生变化，请稍后刷新页面。');
      }
      error = '';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    }
  };

  const selectGallery = async () => {
    if (!batch || sourceWorkRunning) return;
    busy = true;
    selectingSource = true;
    error = '';
    notice = '';
    try {
      const response = await fetch(`${bridge}/v1/source/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (result.cancelled) {
        notice = '已取消选择。';
        return;
      }
      const selectedCount = (result.batch?.items ?? []).length;
      await refresh();
      notice = `已切换图库 · ${selectedCount} 张`;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      selectingSource = false;
      busy = false;
    }
  };

  const startAnalysis = async () => {
    if (!batch || busy || analysis?.running) return;
    busy = true;
    error = '';
    try {
      const response = await fetch(`${bridge}/v1/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      analysis = result.analysis;
      notice = '本地分析已开始。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const startDuplicateScan = async () => {
    if (!batch || busy || duplicates?.running) return;
    busy = true;
    error = '';
    try {
      const response = await fetch(`${bridge}/v1/duplicates/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      duplicates = result.duplicates;
      notice = '正在查找重复照片。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const reviewCategory = async (item: Item, category: string) => {
    if (!batch || !item.versionId || !category || busy) return;
    busy = true;
    try {
      const response = await fetch(`${bridge}/v1/category-review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId, versionId: item.versionId, category }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      batch = result.batch as Batch;
      notice = '分类已保存。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const saveCategories = async () => {
    if (!batch || busy) return;
    const names = [
      ...new Set(
        categoryText
          .split('\n')
          .map((name) => name.trim())
          .filter(Boolean),
      ),
    ];
    if (!names.length || names.length > 80) {
      error = '请至少保留一个分类，最多 80 个。';
      return;
    }
    busy = true;
    try {
      const response = await fetch(`${bridge}/v1/categories`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId, categories: names.map((name) => ({ name })) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      categories = result.categories as Category[];
      categoryText = categories
        .filter((row) => row.enabled)
        .map((row) => row.category)
        .join('\n');
      categoryTextDirty = false;
      notice = '分类预设已保存。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const prepareCodex = async () => {
    if (!batch || codexMode === 'off' || busy) return;
    busy = true;
    codexPreview = null;
    codexConfirmation = '';
    try {
      const response = await fetch(`${bridge}/v1/codex/preview?mode=${codexMode}`, { cache: 'no-store' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      codexPreview = result as CodexPreview;
      notice = `符合条件：${result.targets.length} 张。请核对清单。`;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const sendCodex = async () => {
    if (!batch || !codexPreview || codexConfirmation !== '确认发送到Codex' || busy) return;
    busy = true;
    try {
      const response = await fetch(`${bridge}/v1/codex/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({
          batchId: codexPreview.batchId,
          mode: codexPreview.mode,
          paths: codexPreview.targets.map((item) => item.relativePath),
          confirmation: codexConfirmation,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      codex = result.codex;
      codexPreview = null;
      codexConfirmation = '';
      notice = 'Codex 分析已开始。';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const approve = async () => {
    if (!batch || busy || confirmation !== '确认ExFAT原地改名') return;
    const approvals = Object.fromEntries(
      batch.items
        .filter((item) => selected[item.relativePath])
        .map((item) => [item.relativePath, names[item.relativePath]]),
    );
    busy = true;
    try {
      const response = await fetch(`${bridge}/v1/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId: batch.batchId, approvals, confirmation }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      const successful = result.results.filter((row: { status: string }) => row.status === 'renamed').length;
      const failed = result.results.filter((row: { status: string }) => row.status === 'failed').length;
      notice = `原目录改名 ${successful} 张，失败 ${failed} 张。Immich 正在重扫并核对附加信息。`;
      confirmation = '';
      await refresh();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  onMount(() => {
    void refresh();
    const timer = setInterval(() => {
      if (analysis?.running || duplicates?.running || codex?.running || reconcile?.status === 'running') void refresh();
    }, 4000);
    return () => clearInterval(timer);
  });
</script>

<UserPageLayout title={data.meta.title} scrollbar={true}>
  <div class="mx-auto max-w-7xl space-y-5 p-5 pb-24">
    <PhotoStudioNavigation active="organize" />
    <div class="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <h2 class="text-xl font-semibold">本地照片整理</h2>
      <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
        结果保存在本机；原图不会自动改动。
      </p>
      <div
        class="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200 p-4 dark:border-gray-700"
      >
        <div class="min-w-0 flex-1">
          <p class="text-sm font-semibold">当前图库</p>
          <p class="mt-1 break-all text-sm text-gray-600 dark:text-gray-300" aria-live="polite">
            {batch?.source ?? '连接本地服务后显示'}
          </p>
          <p class="mt-1 text-xs text-gray-500">支持普通文件夹及子文件夹；不支持 Apple「照片」图库。</p>
        </div>
        <button
          class="shrink-0 rounded-lg border border-emerald-700 px-4 py-2 font-semibold text-emerald-800 disabled:opacity-40 dark:text-emerald-300"
          onclick={selectGallery}
          disabled={!batch || sourceWorkRunning}
          aria-label="选择普通照片文件夹">{selectingSource ? '等待选择图库…' : '选择图库'}</button
        >
      </div>
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <button
          class="rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white disabled:opacity-40"
          onclick={startAnalysis}
          disabled={!batch || busy || analysis?.running || duplicates?.running || vision?.running}
          >{analysis?.running ? `本地分析中 ${analysis.completed}/${analysis.total}` : '分析照片'}</button
        >
        <button
          class="rounded-lg border px-4 py-2 disabled:opacity-40"
          onclick={startDuplicateScan}
          disabled={!batch || busy || duplicates?.running || analysis?.running || vision?.running}
          >{duplicates?.running ? `查重中 ${duplicates.completed}/${duplicates.total}` : '查找重复照片'}</button
        >
        {#if batch}<span aria-live="polite">{batch.items.length} 张照片</span>{/if}
      </div>
      {#if analysis && analysis.status !== 'idle'}
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300" aria-live="polite">
          {#if analysis.running}
            Gemma {analysis.engines?.afs?.completed ?? 0}/{analysis.engines?.afs?.total ?? 0}
            · 千问 {analysis.engines?.qwen?.completed ?? 0}/{analysis.engines?.qwen?.total ?? 0}
          {:else if analysis.status === 'interrupted'}
            上次中断 {analysis.completed}/{analysis.total}；重试会继续未完成项。
          {:else if analysis.status === 'partial'}
            分析失败 {analysis.errors.length} 项；成功结果已保留。
          {:else if analysis.status === 'failed'}
            分析未完成；请查看失败项。
          {:else}
            已完成 {analysis.completed}/{analysis.total} 项。
          {/if}
        </p>
      {/if}
      {#if duplicates?.running}
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300" aria-live="polite">
          查重进度 {duplicates.completed}/{duplicates.total}
        </p>
      {/if}
      {#if vision?.running}<p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
          Vision 扫描中 {vision.completed}/{vision.total}（详情见“智能分组”）。
        </p>{/if}
      <details class="mt-4 rounded-xl border p-4">
        <summary class="cursor-pointer font-medium">分类预设</summary>
        <label class="mt-3 block text-sm" for="photo-categories">每行一个分类</label>
        <textarea
          id="photo-categories"
          class="mt-1 min-h-36 w-full rounded-lg border border-gray-300 bg-transparent p-3"
          bind:value={categoryText}
          oninput={() => (categoryTextDirty = true)}></textarea>
        <button
          class="mt-2 rounded-lg border px-4 py-2 disabled:opacity-40"
          onclick={saveCategories}
          disabled={busy || !categoryTextDirty}>保存分类</button
        >
      </details>
      <details class="mt-4 rounded-xl border border-amber-300 p-4 dark:border-amber-700">
        <summary class="cursor-pointer text-sm font-medium">可选：Codex 补充分析（默认关闭）</summary>
        <label class="sr-only" for="codex-mode">Codex 云端补充分析模式</label>
        <div class="mt-2 flex flex-wrap items-center gap-3">
          <select
            id="codex-mode"
            class="rounded-lg border bg-transparent px-3 py-2"
            bind:value={codexMode}
            onchange={() => {
              codexPreview = null;
              codexConfirmation = '';
            }}
          >
            <option value="off">关闭（默认，仅本地）</option>
            <option value="all">全量：本批所有尚无 Codex 结果的图片</option>
            <option value="fallback">兜底：本地结果缺失或主题冲突</option>
          </select>
          <button
            class="rounded-lg border px-4 py-2 disabled:opacity-40"
            onclick={prepareCodex}
            disabled={!batch || codexMode === 'off' || busy || codex?.running}>预览发送清单</button
          >
          {#if codex?.running}<span class="text-sm">Codex 分析中：{codex.completed}/{codex.total}</span>{/if}
        </div>
        {#if codexPreview}
          <div class="mt-3 space-y-2 rounded-lg bg-amber-50 p-3 text-sm dark:bg-gray-800">
            <p>将发送 {codexPreview.targets.length} 张缩小预览图到 Codex（云端）。</p>
            <div class="max-h-44 overflow-y-auto">
              {#each codexPreview.targets as target}<p class="break-all">
                  {target.relativePath}：{target.reason}
                </p>{/each}
            </div>
            {#if codexPreview.targets.length}
              <div class="flex flex-wrap gap-2">
                <input
                  class="rounded-lg border px-3 py-2"
                  placeholder="输入：确认发送到Codex"
                  bind:value={codexConfirmation}
                />
                <button
                  class="rounded-lg bg-amber-700 px-4 py-2 text-white disabled:opacity-40"
                  onclick={sendCodex}
                  disabled={busy || codex?.running || codexConfirmation !== '确认发送到Codex'}
                  >确认发送本批预览图</button
                >
              </div>
            {/if}
          </div>
        {/if}
        {#if codex?.errors.length}<p class="mt-2 text-sm text-red-700">
            Codex 分析失败 {codex.errors.length} 项。
          </p>{/if}
      </details>
      {#if error}<p class="mt-3 text-red-600" role="alert">{error}</p>{/if}
      {#if notice}<p class="mt-3 text-amber-700 dark:text-amber-300" role="status">{notice}</p>{/if}
      {#if analysis?.errors.length}<p class="mt-2 text-sm text-amber-700">
          分析失败 {analysis.errors.length} 项；成功结果已保留。
        </p>{/if}
      {#if analysis?.errors.length}
        <div class="mt-1 space-y-1 text-xs text-amber-800">
          {#each analysis.errors.slice(0, 5) as failure}
            <p class="break-all">{failure.file}{failure.engine ? ` · ${failure.engine}` : ''}：{failure.error}</p>
          {/each}
        </div>
      {/if}
      {#if duplicates?.errors.length}<p class="mt-2 text-sm text-amber-700">
          查重失败 {duplicates.errors.length} 项；失败项不计为重复。
        </p>{/if}
      {#if duplicates?.errors.length}
        <div class="mt-1 space-y-1 text-xs text-amber-800">
          {#each duplicates.errors.slice(0, 5) as failure}
            <p class="break-all">{failure.relativePath ?? failure.file ?? '任务'}：{failure.error}</p>
          {/each}
        </div>
      {/if}
      {#if reconcile?.status === 'needs_attention'}<p class="mt-2 text-sm text-red-700">
          上次改名后的 Immich 信息核对需要处理：{reconcile.error ?? ''}
        </p>{/if}
    </div>

    <section
      class="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900"
    >
      <div>
        <h3 class="text-lg font-semibold">按组审核重复候选</h3>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
          完全相同 {duplicateCounts.exact} 组 · 画面相似 {duplicateCounts.visual} 组 · 待审核 {duplicateCounts.pending} 组
        </p>
        {#if duplicates?.running || duplicates?.status === 'complete'}
          <p class="mt-1 text-xs text-gray-500">
            {duplicates?.running
              ? `只读查重中 ${duplicates.completed}/${duplicates.total}`
              : '查重已完成'}
          </p>
        {/if}
      </div>
      <a class="rounded-lg bg-emerald-700 px-4 py-2 font-semibold text-white" href="/duplicates">打开去重审核</a>
    </section>

    {#if sourceCapabilities.immichRename}
      <details class="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
        <summary class="cursor-pointer text-lg font-semibold">可选：批量改名（默认关闭）</summary>
        <label class="mt-3 flex items-center gap-3 text-sm">
          <input class="h-5 w-5" type="checkbox" bind:checked={renameEnabled} />
          显示改名建议和审批控件
        </label>
        {#if renameEnabled}
          <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
            逐项选择并确认后原地改名，不移动文件。首次最多 2 张。
          </p>
          <button class="mt-3 rounded-lg border px-4 py-2" onclick={() => (selected = {})} disabled={!selectedCount}
            >清除改名选择</button
          >
          {#if reconcile?.status === 'running'}<p class="mt-2 text-sm">Immich 正在核对改名后的信息。</p>{/if}
          {#if reconcile?.status === 'verified'}<p class="mt-2 text-sm text-green-700">
              Immich 附加信息核对完成。
            </p>{/if}
          {#if reconcile?.results.length}
            <details class="mt-2 text-xs">
              <summary class="cursor-pointer">逐项核对结果（{reconcile.results.length}）</summary>
              {#each reconcile.results as row}<p class="mt-1 break-all">
                  {row.old} → {row.new}：{row.status === 'verified' ? '已核对' : '需要处理'}
                </p>{/each}
            </details>
          {/if}
        {/if}
      </details>
    {:else if batch}
      <p
        class="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:bg-gray-900 dark:text-amber-200"
      >
        当前图库不是 Immich 外部图库，改名审批不可用。
      </p>
    {/if}

    <div class="grid gap-4">
      {#each visibleItems as item (item.relativePath)}
        <article
          class={`grid gap-4 rounded-2xl border bg-white p-4 shadow-sm md:grid-cols-[256px_1fr] dark:bg-gray-900 ${selected[item.relativePath] ? 'border-blue-500 ring-2 ring-blue-500' : 'border-gray-200 dark:border-gray-700'}`}
        >
          <button
            type="button"
            class="relative flex h-64 w-full items-center justify-center overflow-hidden rounded-xl bg-gray-100 focus-visible:outline focus-visible:outline-4 focus-visible:outline-blue-500 disabled:cursor-default disabled:opacity-100"
            aria-label={renameEnabled
              ? `${selected[item.relativePath] ? '取消改名选择' : '选择改名'} ${item.originalName}`
              : item.originalName}
            aria-pressed={Boolean(selected[item.relativePath])}
            disabled={!renameEnabled}
            onclick={() => renameEnabled && toggleSelected(item.relativePath)}
          >
            {#if /\.(jpe?g|png|heic|tiff?|webp)$/i.test(item.originalName)}
              <img
                class="h-full w-full object-contain"
                src={`${bridge}/v1/thumbnail/${encodeURIComponent(item.relativePath)}`}
                alt=""
                loading="lazy"
              />
            {:else}
              <span class="text-sm text-gray-600">无预览</span>
            {/if}
            {#if renameEnabled}<span
                class="absolute bottom-3 left-3 rounded-lg bg-white/95 px-3 py-2 text-sm font-semibold shadow"
              >
                {selected[item.relativePath] ? '✓ 已选择改名' : '点图选择改名'}
              </span>{/if}
          </button>
          <div class="min-w-0 space-y-2">
            <div class="flex flex-wrap items-center gap-2 text-sm">
              <span class="rounded-full bg-blue-50 px-3 py-1 text-blue-800 dark:bg-blue-950 dark:text-blue-200"
                >{item.virtualGroup}</span
              >
              {#if item.vision?.needsReview}<span class="text-amber-700">分类待确认</span>{/if}
              {#each item.vision?.tags ?? [] as tag}<span class="text-gray-500">{tag}</span>{/each}
            </div>
            <label class="block text-sm">
              分类
              <select
                class="mt-1 min-h-11 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2"
                value={item.humanCategory ?? item.category}
                onchange={(event) => void reviewCategory(item, (event.currentTarget as HTMLSelectElement).value)}
                disabled={busy || !item.versionId}
              >
                <option value="">待确认</option>
                {#each categories.filter((row) => row.enabled) as category}
                  <option value={category.category}>{category.category}</option>
                {/each}
              </select>
            </label>
            {#if item.captureTime.value}
              <p class="text-sm text-gray-600 dark:text-gray-300">
                拍摄时间：{item.captureTime.value} · {captureSource(item.captureTime.source)}
              </p>
            {/if}
            {#if !item.captureTime.value}
              <p class="text-sm text-gray-600 dark:text-gray-300">
                未读到拍摄时间；创建：{item.fileTimes.created ?? '未知'} · 修改：{item.fileTimes.modified ?? '未知'}
              </p>
            {/if}
            {#if item.personName}<p class="text-sm text-gray-600 dark:text-gray-300">
                已命名人物：{item.personName}
              </p>{/if}
            {#if item.vision}<p class="text-sm text-gray-600 dark:text-gray-300">{item.vision.reason}</p>{/if}
            <p class="break-all text-sm"><span class="text-gray-500">原名：</span>{item.originalName}</p>
            {#if item.identityNeedsReview}<p class="text-sm text-amber-700">文件身份变化，旧结果没有自动合并。</p>{/if}
            {#if renameEnabled}
              <label class="block text-sm">
                建议名称（原目录）
                <input
                  class="mt-1 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2"
                  bind:value={names[item.relativePath]}
                  spellcheck="false"
                />
              </label>
              {#if item.issue}<p class="text-sm text-amber-700">{item.issue}</p>{/if}
            {/if}
            <div class="grid gap-3 pt-2 text-sm lg:grid-cols-3">
              {#if item.models.afs?.short?.trim() || item.models.afs?.translatedShort?.trim() || item.models.afs?.extended?.trim() || item.models.afs?.category || item.models.afs?.long?.trim()}
                <div class="space-y-1 rounded-lg bg-gray-50 p-3 dark:bg-gray-800">
                  <strong>AI File Sorter / Gemma</strong>
                  {#if item.models.afs?.category}<p>类别：{item.models.afs.category}</p>{/if}
                  {#if item.models.afs?.details?.subcategory}<p>
                      细分：{String(item.models.afs.details.subcategory)}
                    </p>{/if}
                  {#if item.models.afs?.short?.trim()}<p>{item.models.afs.short}</p>{/if}
                  {#if item.models.afs?.translatedShort?.trim()}<p>{item.models.afs.translatedShort}</p>{/if}
                  {#if item.models.afs?.extended?.trim()}<p>{item.models.afs.extended}</p>{/if}
                  {#if item.models.afs?.long?.trim()}<p>{item.models.afs.long}</p>{/if}
                  {#if item.models.afs?.details?.fromCache}<p class="text-xs text-gray-500">来自本地缓存</p>{/if}
                </div>
              {/if}
              {#if item.models.qwen?.short?.trim() || item.models.qwen?.long?.trim() || item.models.qwen?.category}
                <div class="space-y-1 rounded-lg bg-gray-50 p-3 dark:bg-gray-800">
                  <strong>Qwen3-VL</strong>
                  {#if item.models.qwen?.category}<p>类别：{item.models.qwen.category}</p>{/if}
                  {#if item.models.qwen?.short?.trim()}<p>{item.models.qwen.short}</p>{/if}
                  {#if item.models.qwen?.long?.trim()}<p>{item.models.qwen.long}</p>{/if}
                  {#if item.models.qwen?.details?.screenshot}<p>
                      截图：{String(item.models.qwen.details.screenshot)}
                    </p>{/if}
                  {#if item.models.qwen?.details?.people}<p>人数：{String(item.models.qwen.details.people)}</p>{/if}
                  {#if item.models.qwen?.details?.uncertainty}<p>
                      不确定：{String(item.models.qwen.details.uncertainty)}
                    </p>{/if}
                </div>
              {/if}
              {#if item.models.codex?.short?.trim() || item.models.codex?.long?.trim()}
                <div class="space-y-1 rounded-lg bg-gray-50 p-3 dark:bg-gray-800">
                  <strong>Codex</strong>
                  {#if item.models.codex?.short?.trim()}<p>{item.models.codex.short}</p>{/if}
                  {#if item.models.codex?.long?.trim()}<p>{item.models.codex.long}</p>{/if}
                </div>
              {/if}
            </div>
          </div>
        </article>
      {/each}
    </div>

    {#if batch && renameEnabled}
      <div class="sticky bottom-0 rounded-2xl border border-gray-300 bg-white p-4 shadow-lg dark:bg-gray-900">
        <div class="flex flex-wrap items-center gap-3">
          <span class="text-sm font-medium">已选 {selectedCount} 张</span>
          {#if firstBatchTooLarge}<span class="text-sm text-red-700">首次最多选 2 张</span>{/if}
          <input class="rounded-lg border px-3 py-2" placeholder="输入：确认ExFAT原地改名" bind:value={confirmation} />
          <button
            class="rounded-lg bg-red-700 px-4 py-2 text-white disabled:opacity-40"
            disabled={busy ||
              selectedCount === 0 ||
              firstBatchTooLarge ||
              confirmation !== '确认ExFAT原地改名' ||
              analysis?.running ||
              codex?.running ||
              reconcile?.status === 'running' ||
              reconcile?.status === 'needs_attention'}
            onclick={approve}>批准改名</button
          >
        </div>
      </div>
    {/if}
  </div>
</UserPageLayout>
