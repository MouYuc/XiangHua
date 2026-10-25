import { computed, ref } from 'vue'

export type Page = 'workbench' | 'voices' | 'wizard' | 'workshop' | 'history'

export type PageMeta = {
  key: Page
  label: string
  crumb: string
}

export const PAGE_META: PageMeta[] = [
  { key: 'workbench', label: '工作台', crumb: '工作台' },
  { key: 'voices', label: '声音库', crumb: '声音库' },
  { key: 'wizard', label: '新建声音', crumb: '声音库 · 新建声音' },
  { key: 'workshop', label: '声音工作坊', crumb: '声音库 · 声音工作坊' },
  { key: 'history', label: '记录', crumb: '记录' },
]

/** 侧栏只出现三个主目的地；wizard/workshop 是声音库的子流程。 */
export const NAV_PAGES: Page[] = ['workbench', 'voices', 'history']

const KEYS = PAGE_META.map((item) => item.key)

function readPage(): Page {
  if (typeof window === 'undefined') return 'workbench'
  const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0]
  return (KEYS as string[]).includes(raw) ? (raw as Page) : 'workbench'
}

const page = ref<Page>(readPage())

if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => {
    page.value = readPage()
  })
}

function setPage(next: Page) {
  page.value = next
  if (typeof window === 'undefined') return
  const target = `#/${next}`
  if (window.location.hash !== target) window.location.hash = target
}

export function usePage() {
  const meta = computed(() => PAGE_META.find((item) => item.key === page.value) ?? PAGE_META[0])
  const isVoiceSubFlow = computed(() => page.value === 'wizard' || page.value === 'workshop')
  return { page, meta, setPage, pages: PAGE_META, navPages: NAV_PAGES, isVoiceSubFlow }
}
