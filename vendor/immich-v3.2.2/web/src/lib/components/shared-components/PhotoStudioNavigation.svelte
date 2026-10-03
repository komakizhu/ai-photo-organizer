<script lang="ts">
  type Section = 'home' | 'organize' | 'groups' | 'duplicates' | 'apple';

  let { active }: { active: Section } = $props();

  const links: { id: Section; href: string; label: string }[] = [
    { id: 'home', href: '/photo-studio', label: '整理中心' },
    { id: 'organize', href: '/organize', label: '本地整理' },
    { id: 'groups', href: '/photo-groups', label: '智能分组' },
    { id: 'duplicates', href: '/duplicates', label: '重复审核' },
    { id: 'apple', href: '/apple-photos', label: 'Apple 图库' },
  ];
</script>

<nav
  class="flex flex-wrap items-center gap-2 rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900"
  aria-label="照片整理页面"
>
  {#each links as link (link.id)}
    <a
      href={link.href}
      aria-current={active === link.id ? 'page' : undefined}
      class={`rounded-full px-4 py-2 text-sm font-medium transition ${
        active === link.id
          ? 'bg-[#123c31] text-white'
          : 'border border-gray-200 text-gray-700 hover:bg-gray-100 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800'
      }`}
    >
      {link.label}
    </a>
  {/each}
</nav>
