<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import UserPageLayout from '$lib/components/layouts/UserPageLayout.svelte';
  import PhotoStudioNavigation from '$lib/components/shared-components/PhotoStudioNavigation.svelte';
  import type { PageData } from './$types';

  type AppleAsset = {
    assetId: string;
    creationDate: string | null;
    width: number;
    height: number;
    screenshot: boolean;
  };
  type Connection = { connected: boolean; authorization: string; mode?: 'native' | 'mock'; message?: string; error?: string };
  type AssetResponse = { assets: AppleAsset[]; nextCursor: Record<string, unknown> | null; hasMore: boolean };

  let { data }: { data: PageData } = $props();
  const bridge = 'http://127.0.0.1:8768';
  let connection = $state<Connection>({ connected: false, authorization: 'unknown' });
  let token = $state('');
  let assets = $state<AppleAsset[]>([]);
  let thumbnails = $state<Record<string, string>>({});
  let thumbnailErrors = $state<Record<string, string>>({});
  let cursor = $state<Record<string, unknown> | null>(null);
  let hasMore = $state(false);
  let screenshotsOnly = $state(false);
  let startDate = $state('');
  let endDate = $state('');
  let busy = $state(false);
  let loadingAssets = $state(false);
  let error = $state('');
  let hasQueried = $state(false);
  let requestVersion = 0;
  const objectUrls = new Set<string>();

  function expireSession(message: string) {
    requestVersion += 1;
    loadingAssets = false;
    token = '';
    assets = [];
    cursor = null;
    hasMore = false;
    hasQueried = false;
    clearThumbnails();
    connection = { connected: false, authorization: 'unknown', message: '照片权限或会话已失效，请重新连接。' };
    error = message || '照片权限或会话已失效，请重新连接。';
  }

  async function call<T>(path: string, body?: unknown, session = token): Promise<T> {
    const response = await fetch(`${bridge}/v1/${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(session ? { 'X-Photo-Studio-Token': session } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const detail = (await response.json().catch(() => ({}))) as { error?: string };
      if (response.status === 403 && session) expireSession(detail.error ?? '照片权限或会话已失效。');
      throw new Error(detail.error ?? `本地服务返回 ${response.status}`);
    }
    return (await response.json()) as T;
  }

  async function readStatus() {
    try {
      const status = await call<Connection>('status');
      connection = status.connected && !token
        ? { ...status, connected: false, message: status.mode === 'mock' ? '模拟预览：点击连接仅展示生成图片，不会请求系统权限。' : '照片权限已授权；点击连接以建立新的页面会话。' }
        : status;
      if (connection.error) error = connection.error;
      if (!connection.connected) {
        token = '';
        assets = [];
        cursor = null;
        hasMore = false;
        clearThumbnails();
      }
    } catch {
      connection = { connected: false, authorization: 'unknown', message: '本地 Apple 图库服务未运行。' };
    }
  }

  async function connect() {
    busy = true;
    error = '';
    try {
      await readStatus();
      const result = await call<Connection & { token?: string }>('connect', {}, '');
      connection = result;
      token = result.token ?? '';
      assets = [];
      cursor = null;
      hasMore = false;
      if (!result.connected) error = result.message ?? '尚未获得照片图库权限。';
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '连接 Apple 图库失败';
      connection = { connected: false, authorization: 'unknown' };
    } finally {
      busy = false;
    }
  }

  function clearThumbnails() {
    for (const url of objectUrls) URL.revokeObjectURL(url);
    objectUrls.clear();
    thumbnails = {};
    thumbnailErrors = {};
  }

  async function disconnect() {
    requestVersion += 1;
    if (token) await call('disconnect', {}, token).catch(() => undefined);
    token = '';
    assets = [];
    cursor = null;
    hasMore = false;
    clearThumbnails();
    connection = { connected: false, authorization: 'unknown', message: '已断开 Apple 图库。' };
  }

  async function loadAssets(append = false) {
    if (!token || loadingAssets) return;
    const version = ++requestVersion;
    loadingAssets = true;
    error = '';
    try {
      if (!append) await call('cancel', {}, token);
      if (version !== requestVersion) return;
      const result = await call<AssetResponse>(
        'assets',
        {
          screenshots: screenshotsOnly,
          startDate: startDate || null,
          endDate: endDate || null,
          cursor: append ? cursor : null,
        },
      );
      if (version !== requestVersion) return;
      if (!append) {
        assets = result.assets;
        clearThumbnails();
        hasQueried = true;
      } else {
        assets = [...assets, ...result.assets];
      }
      cursor = result.nextCursor;
      hasMore = result.hasMore;
      for (const asset of result.assets) void loadThumbnail(asset, version);
    } catch (cause) {
      if (version === requestVersion) error = cause instanceof Error ? cause.message : '读取照片失败';
    } finally {
      if (version === requestVersion) loadingAssets = false;
    }
  }

  async function cancelReading() {
    requestVersion += 1;
    loadingAssets = false;
    if (token) await call('cancel', {}, token).catch(() => undefined);
  }

  async function loadThumbnail(asset: AppleAsset, version: number) {
    try {
      const response = await fetch(`${bridge}/v1/thumbnail`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Photo-Studio-Token': token },
        body: JSON.stringify({ assetId: asset.assetId }),
      });
      if (version !== requestVersion) return;
      if (!response.ok) {
        const detail = (await response.json().catch(() => ({}))) as { error?: string };
        if (response.status === 403) {
          expireSession(detail.error ?? '照片权限或会话已失效。');
          return;
        }
        thumbnailErrors[asset.assetId] = detail.error ?? '本地缩略图暂不可用';
        return;
      }
      const url = URL.createObjectURL(await response.blob());
      objectUrls.add(url);
      thumbnails[asset.assetId] = url;
    } catch {
      if (version === requestVersion) thumbnailErrors[asset.assetId] = '本地缩略图暂不可用';
    }
  }

  onMount(() => {
    void readStatus();
    window.addEventListener('focus', readStatus);
    return () => window.removeEventListener('focus', readStatus);
  });
  onDestroy(() => { clearThumbnails(); });
</script>

<UserPageLayout title={data.meta.title} scrollbar={true}>
  <div class="mx-auto max-w-7xl space-y-6 p-5 pb-24">
    <header class="overflow-hidden rounded-3xl bg-[#123c31] p-6 text-white shadow-sm md:p-8">
      <p class="text-sm font-medium text-emerald-200">本机 PhotoKit · 只读访问</p>
      <h2 class="mt-2 text-3xl font-semibold tracking-tight">Apple 照片图库</h2>
      <p class="mt-2 max-w-3xl text-sm text-emerald-50">
        照片只在你主动连接并点击加载后读取。缩略图不从 iCloud 下载；截图标签使用 Apple Photos 的系统识别结果。
      </p>
    </header>

    <PhotoStudioNavigation active="apple" />

    <section class="space-y-5 rounded-3xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900" aria-label="Apple 图库连接和筛选">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h3 class="font-semibold">图库连接{connection.mode === 'mock' ? ' · 模拟数据' : connection.mode === 'native' ? ' · 本机 Apple 照片' : ''}</h3>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{connection.message ?? '本地服务状态尚未读取。'}</p>
        </div>
        {#if connection.connected}
          <button class="rounded-full border border-gray-300 px-4 py-2 text-sm font-semibold dark:border-gray-600" onclick={disconnect}>
            断开图库
          </button>
        {:else}
          <button class="rounded-full bg-[#123c31] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={busy} onclick={connect}>
            {busy ? (connection.mode === 'mock' ? '连接模拟图库…' : '等待 macOS 授权…') : connection.mode === 'mock' ? '连接模拟图库' : '连接 Apple 图库'}
          </button>
        {/if}
      </div>

      {#if connection.authorization === 'denied'}
        <p class="rounded-xl bg-amber-50 p-3 text-sm text-amber-900">请在“系统设置 → 隐私与安全性 → 照片”中允许 Apple Photos Bridge 访问。</p>
      {/if}

      {#if connection.connected}
        <div class="flex flex-wrap items-end gap-4 border-t border-gray-200 pt-5 dark:border-gray-700">
          <label class="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" bind:checked={screenshotsOnly} />
            只看系统标记的截图
          </label>
          <label class="grid gap-1 text-xs font-medium text-gray-600 dark:text-gray-300">
            开始日期
            <input class="rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800" type="date" bind:value={startDate} />
          </label>
          <label class="grid gap-1 text-xs font-medium text-gray-600 dark:text-gray-300">
            结束日期（含当天）
            <input class="rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800" type="date" bind:value={endDate} />
          </label>
          <button class="rounded-full bg-[#123c31] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={loadingAssets} onclick={() => loadAssets(false)}>
            {loadingAssets ? '读取中…' : assets.length ? '应用筛选' : '加载照片'}
          </button>
          {#if loadingAssets}
            <button class="rounded-full border border-gray-300 px-4 py-2 text-sm font-semibold dark:border-gray-600" onclick={cancelReading}>取消读取</button>
          {/if}
        </div>
      {/if}
    </section>

    {#if error}
      <p role="alert" class="rounded-xl bg-red-50 p-4 text-sm text-red-800">{error}</p>
    {/if}

    {#if assets.length}
      <section class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5" aria-label="Apple 照片">
        {#each assets as asset (asset.assetId)}
          <article class="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
            {#if thumbnails[asset.assetId]}
              <img class="aspect-square w-full object-cover" src={thumbnails[asset.assetId]} alt="Apple 照片缩略图" />
            {:else}
              <div class="flex aspect-square items-center justify-center bg-gray-100 p-4 text-center text-xs text-gray-500 dark:bg-gray-800">
                {thumbnailErrors[asset.assetId] ?? '正在读取本地缩略图…'}
              </div>
            {/if}
            <div class="space-y-1 p-3 text-xs text-gray-600 dark:text-gray-300">
              <p>{asset.creationDate ? new Date(asset.creationDate).toLocaleString() : '拍摄日期未知'}</p>
              <p>{asset.width} × {asset.height}{asset.screenshot ? ' · 系统截图' : ''}</p>
            </div>
          </article>
        {/each}
      </section>
      {#if hasMore}
        <div class="text-center">
          <button class="rounded-full border border-gray-300 px-5 py-2 text-sm font-semibold disabled:opacity-50 dark:border-gray-600" disabled={loadingAssets} onclick={() => loadAssets(true)}>
            {loadingAssets ? '读取中…' : '加载更多'}
          </button>
        </div>
      {/if}
    {:else if connection.connected && !loadingAssets && !error && !hasQueried}
      <p class="rounded-2xl border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700">
        点击“加载照片”后显示图库结果。尚未查询时不会枚举照片。
      </p>
    {:else if connection.connected && hasQueried && !loadingAssets && !error}
      <p class="rounded-2xl border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700">
        当前筛选条件没有匹配的照片。
      </p>
    {/if}
  </div>
</UserPageLayout>
