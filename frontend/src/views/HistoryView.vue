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

/** 记录来自不同批次：按 time 前缀（MM-DD）分组。 */
const groups = computed(() => {
  const map = new Map<string, HistoryEntry[]>()
  for (const entry of entries.value) {
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
    <div class="page-h">
      <div>
        <h1>记录</h1>
        <p class="sub">工作台每次生成的历史都留在这里，随时回听、下载。</p>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn-secondary" type="button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
        <button class="btn-secondary" style="color:var(--err);border-color:var(--err-bd)" type="button" :disabled="clearing" @click="clear">
          {{ confirmClear ? '再点一次确认清空' : '清空记录' }}
        </button>
      </div>
    </div>

    <div style="border:1px solid var(--border);border-radius:10px;background:var(--surface);overflow:hidden">
      <table>
        <thead>
          <tr><th>时间</th><th>内容</th><th>参数</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <template v-for="group in groups" :key="group.day">
            <tr>
              <td colspan="5" style="background:var(--surface2);font-size:11px;font-weight:600;color:var(--text3);letter-spacing:.4px">{{ group.day }}</td>
            </tr>
            <tr v-for="(entry, index) in group.rows" :key="`${entry.time}-${index}`" :class="{ off: !playable(entry) }">
              <td class="mono" style="color:var(--text3);white-space:nowrap">{{ entry.time }}</td>
              <td><span class="cell-text" :title="entry.text">{{ entry.text || entry.name }}</span></td>
              <td class="mono" style="color:var(--text3);font-size:10.5px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ entry.params }}</td>
              <td>
                <span v-if="playable(entry)" class="tag ok">完成</span>
                <span v-else class="tag mut">文件缺失</span>
              </td>
              <td style="text-align:right;white-space:nowrap">
                <template v-if="playable(entry)">
                  <AudioPlayer :src="entry.url" compact :title="entry.name" />
                  <a class="btn-ghost" :href="entry.url" :download="entry.name" title="下载">
                    <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
                  </a>
                </template>
                <span v-else style="font-size:11px;color:var(--text3)">记录保留</span>
              </td>
            </tr>
          </template>
          <tr v-if="!entries.length && !loading">
            <td colspan="5" style="text-align:center;padding:26px;color:var(--text3);font-size:12.5px">
              还没有合成记录。回<button class="linklike" @click="setPage('workbench')">工作台</button>生成第一句试试。
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.cell-text{display:block;max-width:340px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.linklike{color:var(--text);text-decoration:underline;font-size:inherit;padding:0 2px}
</style>
