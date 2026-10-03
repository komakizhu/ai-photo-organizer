<script lang="ts">
  import { onMount } from 'svelte';
  import UserPageLayout from '$lib/components/layouts/UserPageLayout.svelte';
  import PhotoStudioNavigation from '$lib/components/shared-components/PhotoStudioNavigation.svelte';
  import type { PageData } from './$types';

  type Kind = 'exact' | 'visual';
  type Decision = 'same' | 'different' | 'uncertain' | 'discard' | '';
  type Member = {
    versionId: string;
    relativePath: string;
    name: string;
    size: number;
    signature: Record<string, unknown>;
  };
  type Review = {
    kind: Kind;
    members: string[];
    status: Exclude<Decision, ''>;
    keepVersionIds: string[];
    updatedAt: string;
  };
  type Group = {
    kind: Kind;
    versionIds: string[];
    members: Member[];
    edges: { score: number; decision: string; reason: Record<string, unknown> }[];
    review: Review | null;
  };
  type Draft = { status: Decision; keepVersionIds: string[] };
  type LegacyDecision = {
    pair_id: string;
    path_a: string;
    path_b: string;
    decision: 'confirmed' | 'not_duplicate';
  };

  let { data }: { data: PageData } = $props();
  const bridge = import.meta.env.VITE_PHOTO_STUDIO_BRIDGE || 'http://127.0.0.1:8766';
  let batchId = $state('');
  let token = $state('');
  let kind = $state<Kind>('exact');
  let groups = $state<Group[]>([]);
  let legacyDecisions = $state<LegacyDecision[]>([]);
  let drafts = $state<Record<string, Draft>>({});
  let query = $state('');
  let filter = $state<'all' | 'pending' | 'reviewed'>('all');
  let page = $state(0);
  let viewMode = $state<'fit' | 'tile'>('fit');
  let preview = $state<{ groupKey: string; index: number } | null>(null);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let batchRefreshRequest = 0;
  let groupLoadRequest = 0;

  const groupKey = (group: Group) => `${group.kind}:${group.versionIds.slice().sort().join('|')}`;
  const draftFor = (group: Group) =>
    drafts[groupKey(group)] ?? {
      status: group.review?.status ?? '',
      keepVersionIds: group.review?.keepVersionIds ?? [],
    };
  const previewGroup = $derived.by(() => {
    const current = preview;
    return current ? (groups.find((group) => groupKey(group) === current.groupKey) ?? null) : null;
  });
  const previewMember = $derived.by(() => {
    const current = preview;
    const group = previewGroup;
    return current && group ? (group.members[current.index] ?? null) : null;
  });
  const previewUrl = $derived(
    previewMember ? `${bridge}/v1/preview/${encodeURIComponent(previewMember.relativePath)}` : '',
  );
  const filteredGroups = $derived(
    groups.filter((group) => {
      const draft = draftFor(group);
      const reviewed = Boolean(group.review);
      if (filter === 'pending' && reviewed) return false;
      if (filter === 'reviewed' && !reviewed) return false;
      const needle = query.trim().toLocaleLowerCase();
      return (
        !needle ||
        group.members.some((member) => `${member.name} ${member.relativePath}`.toLocaleLowerCase().includes(needle)) ||
        (draft.status && draft.status.toLocaleLowerCase().includes(needle))
      );
    }),
  );
  const pages = $derived.by(() => {
    const result: Group[][] = [];
    let current: Group[] = [];
    let photos = 0;
    for (const group of filteredGroups) {
      const exceedsGroups = current.length >= 6;
      const exceedsPhotos = current.length > 0 && photos + group.members.length > 12;
      if (exceedsGroups || exceedsPhotos) {
        result.push(current);
        current = [];
        photos = 0;
      }
      current.push(group);
      photos += group.members.length;
    }
    if (current.length) result.push(current);
    return result;
  });
  const pageGroups = $derived(pages[page] ?? []);
  const reviewedCount = $derived(groups.filter((group) => Boolean(group.review)).length);
  const pendingCount = $derived(groups.length - reviewedCount);

  const thumbnail = (path: string) => `${bridge}/v1/thumbnail/${encodeURIComponent(path)}`;
  const changeFilter = (value: typeof filter) => {
    filter = value;
    page = 0;
  };
  const setDraft = (group: Group, status: Decision, keepVersionIds: string[]) => {
    drafts[groupKey(group)] = { status, keepVersionIds };
  };
  const chooseDecision = (group: Group, status: Exclude<Decision, ''>) => {
    const keeps = status === 'same' ? draftFor(group).keepVersionIds : [];
    setDraft(group, status, keeps);
  };
  const toggleKeep = (group: Group, versionId: string) => {
    const current = draftFor(group);
    const keeps = new Set(current.keepVersionIds);
    if (keeps.has(versionId)) keeps.delete(versionId);
    else keeps.add(versionId);
    const next = [...keeps];
    setDraft(group, next.length ? 'same' : current.status === 'same' ? '' : current.status, next);
  };

  const loadGroups = async () => {
    const requestId = ++groupLoadRequest;
    const response = await fetch(`${bridge}/v1/duplicate-groups?kind=${kind}`, { cache: 'no-store' });
    const result = await response.json();
    if (requestId !== groupLoadRequest) return;
    if (!response.ok) throw new Error(result.error ?? '读取重复候选失败');
    if (typeof result.batchId === 'string') batchId = result.batchId;
    groups = (result.groups ?? []) as Group[];
    legacyDecisions = (result.legacyDecisions ?? []) as LegacyDecision[];
    for (const group of groups) {
      const key = groupKey(group);
      if (!(key in drafts)) {
        drafts[key] = {
          status: group.review?.status ?? '',
          keepVersionIds: group.review?.keepVersionIds ?? [],
        };
      }
    }
    if (page >= pages.length) page = Math.max(0, pages.length - 1);
  };

  const refresh = async () => {
    const requestId = ++batchRefreshRequest;
    try {
      const response = await fetch(`${bridge}/v1/batch`, { cache: 'no-store' });
      const result = await response.json();
      if (requestId !== batchRefreshRequest) return;
      if (!response.ok) throw new Error(result.error ?? '本地整理服务未连接');
      batchId = result.batch.batchId;
      token = result.token;
      await loadGroups();
      if (requestId !== batchRefreshRequest) return;
      error = '';
    } catch (reason) {
      if (requestId !== batchRefreshRequest) return;
      error = reason instanceof Error ? reason.message : String(reason);
    }
  };

  const switchKind = async (value: Kind) => {
    if (kind === value) return;
    kind = value;
    page = 0;
    await loadGroups();
  };

  const savePage = async () => {
    if (!batchId || busy || !pageGroups.length) return;
    const payload = pageGroups.map((group) => ({
      kind: group.kind,
      versionIds: group.versionIds,
      status: draftFor(group).status,
      keepVersionIds: draftFor(group).keepVersionIds,
    }));
    const invalid = payload.find(
      (group) =>
        !group.status ||
        (group.status === 'same' && group.keepVersionIds.length === 0) ||
        (group.status !== 'same' && group.keepVersionIds.length > 0),
    );
    if (invalid) {
      error = '请为本页每组选择判断；确认“同图”时至少选择一张保留。';
      return;
    }
    busy = true;
    error = '';
    try {
      const response = await fetch(`${bridge}/v1/duplicate-groups/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ batchId, groups: payload }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? '保存审核结果失败');
      for (const submitted of payload) {
        const key = `${submitted.kind}:${submitted.versionIds.slice().sort().join('|')}`;
        drafts[key] = { status: submitted.status, keepVersionIds: submitted.keepVersionIds };
      }
      await loadGroups();
      notice = `已保存 ${payload.length} 组。`;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  };

  const togglePageSelection = (select: boolean) => {
    for (const group of pageGroups) {
      const keepVersionIds = select ? group.versionIds.slice() : [];
      setDraft(
        group,
        select ? 'same' : draftFor(group).status === 'same' ? '' : draftFor(group).status,
        keepVersionIds,
      );
    }
  };

  const openPreview = (group: Group, index: number) => {
    preview = { groupKey: groupKey(group), index };
  };
  const stepPreview = (delta: number) => {
    if (!previewGroup || !preview) return;
    const count = previewGroup.members.length;
    preview = { ...preview, index: (preview.index + delta + count) % count };
  };

  onMount(() => {
    viewMode = localStorage.getItem('photo-studio-duplicate-view') === 'tile' ? 'tile' : 'fit';
    void refresh();
    const timer = setInterval(() => void refresh(), 8000);
    return () => clearInterval(timer);
  });
</script>

<svelte:head>
  <title>{data.meta.title}</title>
</svelte:head>

<UserPageLayout title={data.meta.title} scrollbar={true}>
  <div class="mx-auto max-w-7xl space-y-5 p-5 pb-24">
    <PhotoStudioNavigation active="duplicates" />
    <header class="rounded-2xl bg-[#123c31] p-5 text-white shadow-sm">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-sm text-emerald-100">本机审核 · 原图只读</p>
          <h2 class="mt-1 text-2xl font-semibold">重复照片分组审核</h2>
          <p class="mt-2 max-w-2xl text-sm text-emerald-50">
            选保留项，再确认每组判断。
          </p>
        </div>
        <a class="rounded-lg border border-white/40 px-3 py-2 text-sm hover:bg-white/10" href="/photo-studio"
          >返回整理中心</a
        >
      </div>
    </header>

    <section
      class="rounded-2xl border border-stone-200 bg-[#fffdf8] p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
    >
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex gap-2" role="tablist" aria-label="候选类型">
          <button
            class={`rounded-full px-4 py-2 text-sm ${kind === 'exact' ? 'bg-[#123c31] text-white' : 'border border-stone-300 dark:border-gray-600'}`}
            onclick={() => void switchKind('exact')}>完全相同</button
          >
          <button
            class={`rounded-full px-4 py-2 text-sm ${kind === 'visual' ? 'bg-[#123c31] text-white' : 'border border-stone-300 dark:border-gray-600'}`}
            onclick={() => void switchKind('visual')}>画面相似</button
          >
        </div>
        <div class="flex items-center gap-2 text-sm text-stone-600 dark:text-gray-300">
          <span>已审核 {reviewedCount}</span><span>·</span><span>待审核 {pendingCount}</span>
          <button
            class="ml-2 rounded-lg border border-stone-300 px-3 py-2 dark:border-gray-600"
            onclick={() => void refresh()}>刷新</button
          >
        </div>
      </div>
      {#if kind === 'visual'}
        <p class="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          同组成员可能只有间接相似，请逐组判断。
        </p>
      {/if}
      <div class="mt-4 grid gap-3 md:grid-cols-[1fr_auto_auto]">
        <input
          class="rounded-lg border border-stone-300 bg-transparent px-3 py-2 dark:border-gray-600"
          placeholder="搜索文件名或路径"
          bind:value={query}
          oninput={() => (page = 0)}
        />
        <select
          class="rounded-lg border border-stone-300 bg-transparent px-3 py-2 dark:border-gray-600"
          bind:value={filter}
          onchange={() => (page = 0)}
          aria-label="审核状态"
        >
          <option value="all">全部组</option><option value="pending">待审核</option><option value="reviewed"
            >已审核</option
          >
        </select>
        <div class="flex rounded-lg border border-stone-300 p-1 dark:border-gray-600" aria-label="图片显示方式">
          <button
            class={`rounded px-3 py-1 text-sm ${viewMode === 'fit' ? 'bg-stone-200 dark:bg-gray-700' : ''}`}
            onclick={() => {
              viewMode = 'fit';
              localStorage.setItem('photo-studio-duplicate-view', viewMode);
            }}>适应</button
          >
          <button
            class={`rounded px-3 py-1 text-sm ${viewMode === 'tile' ? 'bg-stone-200 dark:bg-gray-700' : ''}`}
            onclick={() => {
              viewMode = 'tile';
              localStorage.setItem('photo-studio-duplicate-view', viewMode);
            }}>平铺</button
          >
        </div>
      </div>
    </section>

    {#if error}<p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">
        {error}
      </p>{/if}
    {#if notice}<p
        class="rounded-lg bg-green-50 p-3 text-sm text-green-800 dark:bg-green-950 dark:text-green-200"
        role="status"
      >
        {notice}
      </p>{/if}
    {#if legacyDecisions.length}
      <details class="rounded-2xl border border-blue-200 bg-blue-50/70 p-4 dark:border-blue-900 dark:bg-blue-950/40">
        <summary class="cursor-pointer text-sm font-semibold">保留的旧逐对判断 · {legacyDecisions.length}</summary>
        <p class="mt-2 text-xs text-stone-600 dark:text-gray-300">
          旧判断不会选择保留项；成员变化后请重新审核。
        </p>
        <div class="mt-3 grid gap-2 sm:grid-cols-2">
          {#each legacyDecisions as pair (pair.pair_id)}
            <p class="break-all text-xs">
              {pair.path_a} ↔ {pair.path_b} · {pair.decision === 'confirmed' ? '已确认候选相同' : '已标记不同'}
            </p>
          {/each}
        </div>
      </details>
    {/if}

    {#if !filteredGroups.length}
      <section
        class="rounded-2xl border border-stone-200 bg-[#fffdf8] p-10 text-center dark:border-gray-700 dark:bg-gray-900"
      >
        <h3 class="text-lg font-semibold">{groups.length ? '没有符合筛选条件的组' : '还没有重复候选'}</h3>
        <p class="mt-2 text-sm text-stone-600 dark:text-gray-300">
          {groups.length ? '调整搜索词或审核状态。' : '先在本地整理页查重。'}
        </p>
        {#if !groups.length}<a class="mt-4 inline-flex rounded-lg bg-[#123c31] px-4 py-2 text-white" href="/organize"
            >前往照片整理</a
          >{/if}
      </section>
    {:else}
      <div class="space-y-4">
        {#each pageGroups as group (groupKey(group))}
          {@const draft = draftFor(group)}
          <article
            class="overflow-hidden rounded-2xl border border-stone-200 bg-[#fffdf8] shadow-sm dark:border-gray-700 dark:bg-gray-900"
          >
            <div
              class="flex flex-wrap items-center justify-between gap-3 border-b border-stone-200 px-4 py-3 dark:border-gray-700"
            >
              <div>
                <h3 class="font-semibold">
                  {group.kind === 'exact' ? '完全相同' : '画面相似'} · {group.members.length} 张
                </h3>
                <p class="text-xs text-stone-600 dark:text-gray-300">
                  {group.kind === 'visual' ? `${group.edges.length} 个相似线索` : '文件内容相同'}
                </p>
                {#if group.edges.some((edge) => edge.decision !== 'suggested')}
                  <p class="mt-1 text-xs text-blue-800 dark:text-blue-200">
                    旧判断：{group.edges.filter((edge) => edge.decision === 'confirmed').length} 对相同，{group.edges.filter(
                      (edge) => edge.decision === 'not_duplicate',
                    ).length} 对不同；请重新选择保留项。
                  </p>
                {/if}
              </div>
              <div class="flex flex-wrap gap-2" aria-label="本组判断">
                <button
                  class={`rounded-full border px-3 py-1.5 text-sm ${draft.status === 'same' ? 'border-emerald-700 bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100' : 'border-stone-300 dark:border-gray-600'}`}
                  onclick={() => chooseDecision(group, 'same')}>同图</button
                >
                <button
                  class={`rounded-full border px-3 py-1.5 text-sm ${draft.status === 'different' ? 'border-blue-700 bg-blue-50 text-blue-900 dark:bg-blue-950 dark:text-blue-100' : 'border-stone-300 dark:border-gray-600'}`}
                  onclick={() => chooseDecision(group, 'different')}>不同</button
                >
                <button
                  class={`rounded-full border px-3 py-1.5 text-sm ${draft.status === 'uncertain' ? 'border-amber-700 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100' : 'border-stone-300 dark:border-gray-600'}`}
                  onclick={() => chooseDecision(group, 'uncertain')}>待定</button
                >
                <button
                  class={`rounded-full border px-3 py-1.5 text-sm ${draft.status === 'discard' ? 'border-rose-700 bg-rose-50 text-rose-900 dark:bg-rose-950 dark:text-rose-100' : 'border-stone-300 dark:border-gray-600'}`}
                  onclick={() => chooseDecision(group, 'discard')}>都不要</button
                >
              </div>
            </div>
            <div class="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
              {#each group.members as member, index (member.versionId)}
                {@const keeping = draft.keepVersionIds.includes(member.versionId)}
                <div
                  class={`overflow-hidden rounded-xl border ${keeping ? 'border-emerald-600 ring-2 ring-emerald-500' : 'border-stone-200 dark:border-gray-700'}`}
                >
                  <div class="relative bg-stone-100 dark:bg-gray-800">
                    <button
                      type="button"
                      class="flex h-56 w-full items-center justify-center overflow-hidden"
                      aria-label={`放大 ${member.name}`}
                      onclick={() => openPreview(group, index)}
                    >
                      <img
                        class={`h-full w-full ${viewMode === 'fit' ? 'object-contain' : 'object-cover'}`}
                        src={thumbnail(member.relativePath)}
                        alt={member.name}
                        loading="lazy"
                      />
                    </button>
                    <button
                      type="button"
                      class={`absolute left-3 top-3 rounded-full px-3 py-1.5 text-sm font-semibold shadow ${keeping ? 'bg-emerald-700 text-white' : 'bg-white/95 text-stone-900'}`}
                      aria-pressed={keeping}
                      onclick={() => toggleKeep(group, member.versionId)}>{keeping ? '✓ 保留' : '＋ 保留'}</button
                    >
                    <button
                      type="button"
                      class="absolute right-3 top-3 rounded-full bg-white/95 px-3 py-1.5 text-sm text-stone-900 shadow"
                      aria-label={`放大预览 ${member.name}`}
                      onclick={() => openPreview(group, index)}>放大</button
                    >
                  </div>
                  <div class="space-y-1 p-3">
                    <p class="break-all text-sm font-medium">{member.name}</p>
                    <p class="break-all text-xs text-stone-600 dark:text-gray-300">{member.relativePath}</p>
                    <p class="text-xs text-stone-500">
                      {(member.size / 1024 / 1024).toFixed(2)} MB{keeping ? ' · 已选为保留项' : ''}
                    </p>
                  </div>
                </div>
              {/each}
            </div>
          </article>
        {/each}
      </div>

      <footer
        class="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-stone-200 bg-[#fffdf8]/95 p-3 shadow-lg backdrop-blur dark:border-gray-700 dark:bg-gray-900/95"
      >
        <div class="flex flex-wrap gap-2">
          <button
            class="rounded-lg border border-stone-300 px-3 py-2 text-sm dark:border-gray-600"
            onclick={() => togglePageSelection(true)}>本页全选</button
          >
          <button
            class="rounded-lg border border-stone-300 px-3 py-2 text-sm dark:border-gray-600"
            onclick={() => togglePageSelection(false)}>本页全不选</button
          >
          <span class="self-center text-sm text-stone-600 dark:text-gray-300"
            >第 {page + 1}/{pages.length} 页 · {pageGroups.reduce((count, group) => count + group.members.length, 0)} 张</span
          >
        </div>
        <div class="flex gap-2">
          <button
            class="rounded-lg border border-stone-300 px-3 py-2 disabled:opacity-40 dark:border-gray-600"
            disabled={page <= 0}
            onclick={() => (page -= 1)}>上一页</button
          >
          <button
            class="rounded-lg border border-stone-300 px-3 py-2 disabled:opacity-40 dark:border-gray-600"
            disabled={page + 1 >= pages.length}
            onclick={() => (page += 1)}>下一页</button
          >
          <button
            class="rounded-lg bg-[#123c31] px-4 py-2 font-semibold text-white disabled:opacity-40"
            disabled={busy}
            onclick={savePage}>{busy ? '保存中…' : '总确定 · 保存本页'}</button
          >
        </div>
      </footer>
    {/if}
  </div>

  {#if preview && previewGroup && previewMember}
    <div
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      role="presentation"
      onclick={(event) => {
        if (event.target === event.currentTarget) preview = null;
      }}
      onkeydown={(event) => {
        if (event.key === 'Escape') preview = null;
      }}
    >
      <button
        class="absolute right-5 top-4 rounded-full bg-white/15 px-4 py-2 text-white"
        aria-label="关闭预览"
        onclick={() => (preview = null)}>关闭 ✕</button
      >
      <button
        class="absolute left-3 rounded-full bg-white/15 px-4 py-3 text-2xl text-white sm:left-8"
        aria-label="上一张"
        onclick={() => stepPreview(-1)}>‹</button
      >
      <figure class="flex h-full max-h-[90vh] w-full max-w-6xl flex-col items-center justify-center gap-3">
        <img class="max-h-[78vh] max-w-full object-contain" src={previewUrl} alt={previewMember.name} />
        <figcaption class="max-w-full break-all text-center text-sm text-white">
          {previewMember.name} · {preview.index + 1}/{previewGroup.members.length}
        </figcaption>
      </figure>
      <button
        class="absolute right-3 rounded-full bg-white/15 px-4 py-3 text-2xl text-white sm:right-8"
        aria-label="下一张"
        onclick={() => stepPreview(1)}>›</button
      >
    </div>
  {/if}
</UserPageLayout>
