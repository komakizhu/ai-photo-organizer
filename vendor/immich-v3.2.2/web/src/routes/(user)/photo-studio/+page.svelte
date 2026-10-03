<script lang="ts">
  import UserPageLayout from '$lib/components/layouts/UserPageLayout.svelte';
  import PhotoStudioNavigation from '$lib/components/shared-components/PhotoStudioNavigation.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const tools = [
    {
      href: '/organize',
      number: '01',
      eyebrow: '本地分析与查重',
      title: '本地照片整理',
      features: ['选择图库', '分类分析', '查重'],
      action: '打开本地整理',
    },
    {
      href: '/photo-groups',
      number: '02',
      eyebrow: 'Apple Vision',
      title: '智能分组',
      features: ['扫描照片', '分类建议', '人物审核'],
      action: '打开智能分组',
    },
    {
      href: '/duplicates',
      number: '03',
      eyebrow: '分组人工审核',
      title: '重复照片审核',
      features: ['相同与相似', '选择保留项', '批量审核'],
      action: '打开重复审核',
    },
    {
      href: '/apple-photos',
      number: '04',
      eyebrow: 'PhotoKit · 只读',
      title: 'Apple 照片图库',
      features: ['缩略图浏览', '截图筛选', '日期范围'],
      action: '连接 Apple 图库',
    },
  ];
</script>

<UserPageLayout title={data.meta.title} scrollbar={true}>
  <div class="mx-auto max-w-7xl space-y-6 p-5 pb-24">
    <header class="overflow-hidden rounded-3xl bg-[#123c31] p-6 text-white shadow-sm md:p-8">
      <p class="text-sm font-medium text-emerald-200">本机工具 · 人工审核 · 原图只读</p>
      <h2 class="mt-2 text-3xl font-semibold tracking-tight">照片整理中心</h2>
    </header>

    <PhotoStudioNavigation active="home" />

    <section class="grid gap-4 lg:grid-cols-4" aria-label="照片整理工具">
      {#each tools as tool (tool.href)}
        <a
          href={tool.href}
          class="group flex min-h-72 flex-col rounded-3xl border border-gray-200 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-emerald-700 hover:shadow-lg dark:border-gray-700 dark:bg-gray-900 dark:hover:border-emerald-500"
        >
          <div class="flex items-center justify-between">
            <span class="text-sm font-semibold tracking-[0.16em] text-emerald-800 dark:text-emerald-300">
              {tool.number}
            </span>
            <span
              class="rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300"
              >{tool.eyebrow}</span
            >
          </div>
          <h3 class="mt-8 text-xl font-semibold">{tool.title}</h3>
          <div class="mt-5 flex flex-wrap gap-2">
            {#each tool.features as feature (feature)}
              <span
                class="rounded-full border border-gray-200 px-3 py-1 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-300"
                >{feature}</span
              >
            {/each}
          </div>
          <span
            class="mt-auto flex items-center gap-2 pt-8 font-semibold text-emerald-800 group-hover:gap-3 dark:text-emerald-300"
          >
            {tool.action}<span aria-hidden="true">→</span>
          </span>
        </a>
      {/each}
    </section>
  </div>
</UserPageLayout>
