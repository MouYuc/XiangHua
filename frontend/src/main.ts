import { createApp } from 'vue'
import App from './App.vue'
import './styles/tokens.css'
import './styles/base.css'

// 调试钩子：任何被吞掉的 JS 错误都会在控制台留下 [xianghua] 痕迹。
window.addEventListener('error', (event) => {
  console.error('[xianghua] window error:', event.message, event.filename, `${event.lineno}:${event.colno}`)
})
window.addEventListener('unhandledrejection', (event) => {
  console.error('[xianghua] unhandled rejection:', event.reason)
})

// 全局点击探针（捕获阶段）：即使某个处理器 stopPropagation，这里也会先记录
// 点击真正落在了哪个元素上。如果连这条都没有，说明点击被浏览器层（扩展等）吃掉。
window.addEventListener(
  'click',
  (event) => {
    if (!(event instanceof MouseEvent)) return
    const describe = (el: Element): string => {
      const cls = String(el.className ?? '').split(' ').filter(Boolean).join('.')
      const text = (el.textContent ?? '').trim().slice(0, 16)
      return `${el.tagName.toLowerCase()}${cls ? `.${cls}` : ''}${text ? ` "${text}"` : ''}`
    }
    const chain: string[] = []
    let node: Element | null = event.target instanceof Element ? event.target : null
    for (let depth = 0; node && depth < 6; depth++) {
      chain.push(describe(node))
      node = node.parentElement
    }
    const x = Math.round(event.clientX)
    const y = Math.round(event.clientY)
    const under = document.elementFromPoint(x, y)
    console.debug(
      `[xianghua] click (${x},${y}) trusted=${event.isTrusted} → ${chain.join(' ← ')}` +
        (under ? ` | 光标下真实元素: ${describe(under)}` : ''),
    )
  },
  true,
)

console.debug('[xianghua] app boot:', import.meta.env.DEV ? 'dev' : 'prod', new Date().toISOString())

createApp(App).mount('#app')
