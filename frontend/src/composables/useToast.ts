import { ref } from 'vue'

export type ToastTone = 'ok' | 'error' | 'info'
export type ToastItem = { id: number; message: string; tone: ToastTone }

const toasts = ref<ToastItem[]>([])
const timers = new Map<number, number>()
let nextId = 0

function dismiss(id: number) {
  toasts.value = toasts.value.filter((item) => item.id !== id)
  const timer = timers.get(id)
  if (timer !== undefined) {
    window.clearTimeout(timer)
    timers.delete(id)
  }
}

function push(message: string, tone: ToastTone = 'info') {
  const text = String(message || '').trim()
  if (!text) return
  const id = ++nextId
  toasts.value = [...toasts.value, { id, message: text, tone }].slice(-4)
  const ttl = tone === 'error' ? 8000 : 4200
  timers.set(id, window.setTimeout(() => dismiss(id), ttl))
}

/** 全局消息队列。页面/组合式函数共用同一份，切页不丢。 */
export function useToast() {
  return {
    toasts,
    push,
    dismiss,
    success: (message: string) => push(message, 'ok'),
    error: (message: string) => push(message, 'error'),
    info: (message: string) => push(message, 'info'),
  }
}
