import { ref } from 'vue'

export type ThemeName = 'light' | 'dark'

const THEME_KEY = 'xianghua.theme'
const MOTION_KEY = 'xianghua.reduce-motion'

function readStored(key: string, fallback: string) {
  try {
    return globalThis.localStorage?.getItem(key) || fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: string) {
  try {
    globalThis.localStorage?.setItem(key, value)
  } catch {
    /* 存储不可用时主题只保留在内存 */
  }
}

function apply(theme: ThemeName) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-theme', theme)
}

function applyMotion(reduced: boolean) {
  if (typeof document === 'undefined') return
  document.documentElement.classList.toggle('reduce-motion', reduced)
}

const theme = ref<ThemeName>(readStored(THEME_KEY, 'light') === 'dark' ? 'dark' : 'light')
const reduceMotion = ref(readStored(MOTION_KEY, '') === '1')

apply(theme.value)
applyMotion(reduceMotion.value)

export function useTheme() {
  function toggleTheme() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    writeStored(THEME_KEY, theme.value)
    apply(theme.value)
  }

  function toggleReduceMotion() {
    reduceMotion.value = !reduceMotion.value
    writeStored(MOTION_KEY, reduceMotion.value ? '1' : '')
    applyMotion(reduceMotion.value)
  }

  return { theme, reduceMotion, toggleTheme, toggleReduceMotion }
}
