<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { clearHistory, fetchHistory, type HistoryEntry } from '@/lib/api'
import { useToast } from '@/composables/useToast'
import { usePage } from '@/composables/usePage'
import AudioPlayer from '@/components/AudioPlayer.vue'

defineOptions({ name: 'HistoryView' })

const toast = useToast()
const { setPage } = usePage()

const entries = ref<HistoryEntry[]>([])
const loading = ref(false)
const clearing = ref(false)
const confirmClear = ref(false)
const search = ref('')

async function load() {
  loading.value = true
  try {
    entries.value = await fetchHistory()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

async function clear() {
  if (!confirmClear.value) {
    confirmClear.value = true
    return
  }
  clearing.value = true
  try {
    await clearHistory()
    entries.value = []
    confirmClear.value = false
    toast.success('已清空合成记录与音频文件。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    clearing.value = false
  }
}

/** 后端没有 audio_url 时（download 凭据缺失）显示占位，不再渲染坏播放器。 */
function playable(entry: HistoryEntry) {
  return Boolean(entry.url)
}

/** 记录来自不同批次：按 time 前缀（MM-DD）分组；页头搜索框可按文件名/内容过滤。 */
const filteredEntries = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  if (!keyword) return entries.value
  return entries.value.filter((entry) => `${entry.name}${entry.text}`.toLowerCase().includes(keyword))
})

const groups = computed(() => {
  const map = new Map<string, HistoryEntry[]>()
  for (const entry of filteredEntries.value) {
    const day = (entry.time || '').split(' ')[0] || '未知时间'
    const list = map.get(day) ?? []
    list.push(entry)
    map.set(day, list)
  }
  return [...map.entries()].map(([day, rows]) => ({ day, rows }))
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page-wrap" style="max-width:860px">
    <div class="h-row">
      <span class="lbl">记录</span>
      <span class="h-count">history · 最近 200 条 · 按 日期 分组</span>
      <span class="r">
        <label class="search-kbtn">
          <svg class="ic sm" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
          <input v-model="search" placeholder="搜索文件 / 内容…">
        </label>
        <button class="mini" type="button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
        <button class="mini danger" type="button" :disabled="clearing" @click="clear">
          {{ confirmClear ? '再点一次确认清空' : '清空记录' }}
        </button>
      </span>
    </div>

    <table class="rec">
      <thead>
        <tr>
          <th style="width:34%">文件</th>
          <th style="width:12%">时长</th>
          <th style="width:30%">状态</th>
          <th style="width:24%">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="group in groups" :key="group.day">
          <tr>
            <td class="grp" colspan="4">{{ group.day }} <span class="c">· {{ group.rows.length }} 条</span></td>
          </tr>
          <tr v-for="(entry, index) in group.rows" :key="`${entry.time}-${index}`" :class="{ off: !playable(entry) }">
            <td>
              <span class="fname" :class="{ dim: !playable(entry) }" :title="entry.text">{{ entry.name || '未命名' }}</span>
              <span v-if="entry.text" class="line-text">{{ entry.text }}</span>
            </td>
            <td class="mono len">—</td>
            <td>
              <span v-if="playable(entry)" class="tag ink">已完成</span>
              <template v-else>
                <span class="tag mut">文件缺失</span>
                <span class="reason">音频文件已清理或下载凭据缺失</span>
              </template>
            </td>
            <td class="acts">
              <template v-if="playable(entry)">
                <AudioPlayer :src="entry.url" compact :title="entry.name" />
                <a class="btn-ghost" :href="entry.url" :download="entry.name" title="下载">
                  <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
                </a>
              </template>
              <span v-else class="mono none">—</span>
            </td>
          </tr>
        </template>
        <tr v-if="!filteredEntries.length && !loading">
          <td colspan="4" style="text-align:center;padding:26px;color:var(--text3);font-size:12.5px">
            还没有合成记录。回<button class="linklike" @click="setPage('workbench')">工作台</button>生成第一句试试。
          </td>
        </tr>
      </tbody>
    </table>
    <div class="rec-note">仅保留最近 200 条，更早记录按保留策略归档。</div>
  </div>
</template>

<style scoped>
/* 屏 3 / 记录（design/D-focus.html table.rec） */
.h-row{display:flex;align-items:center;gap:12px;padding:22px 0 14px;flex-wrap:wrap}
.h-row .r{margin-left:auto;display:flex;align-items:center;gap:10px}
.h-count{font-family:var(--mono);font-size:11px;color:var(--text3)}
.search-kbtn{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);border-radius:var(--rs);padding:5px 10px;width:210px;color:var(--text3);background:var(--surface)}
.search-kbtn:hover{border-color:var(--border2)}
.search-kbtn input{border:none;outline:none;background:none;font:inherit;font-size:12.5px;color:var(--text);width:100%}
.search-kbtn:focus-within{border-color:var(--text2)}
.mini.danger{color:var(--err);border-color:var(--err-bd)}
.mini.danger:hover{background:var(--err-bg);color:var(--err);border-color:var(--err-bd)}

table.rec{width:100%}
table.rec :deep(th){letter-spacing:.14em}
table.rec td{padding:9px 12px}
table.rec td.grp{font-family:var(--mono);font-size:11px;color:var(--text);letter-spacing:.1em;background:var(--surface2);border-bottom:1px solid var(--border2);padding:7px 12px}
table.rec td.grp .c{color:var(--text3)}
.fname{font-family:var(--mono);font-size:12.5px;color:var(--text)}
.fname.dim{color:var(--text3)}
.line-text{display:block;font-size:11.5px;color:var(--text3);max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.len{font-size:12px;color:var(--text2)}
.tag.ink{border-color:var(--text);color:var(--text)}
.reason{font-size:12px;color:var(--text3);margin-left:7px}
td.acts{white-space:nowrap}
td.acts :deep(.audio-player){display:inline-flex;vertical-align:middle;margin-right:6px}
.mono.none{font-size:11px;color:var(--text3)}
.rec-note{font-family:var(--mono);font-size:10.5px;color:var(--text3);padding:14px 0 0}
.linklike{color:var(--text);text-decoration:underline;font-size:inherit;padding:0 2px}
</style>
