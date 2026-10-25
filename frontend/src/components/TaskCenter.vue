<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useJobs, type JobRecord } from '@/composables/useJobs'

const { activeJobs, badge, open, loadFailed, setOpen, refresh, cancel } = useJobs()

const list = computed(() => activeJobs.value)
const rootEl = ref<HTMLElement | null>(null)

function kindIcon(job: JobRecord) {
  if (job.kind === 'synthesis') return 'synth'
  if (job.kind === 'voice_prepare' || job.kind === 'media_analysis') return 'ingest'
  if (job.kind === 'training') return 'train'
  return 'convert'
}

function progressText(job: JobRecord) {
  const stage = job.stage ? `${job.stage} · ` : ''
  const percent = typeof job.progress === 'number' && Number.isFinite(job.progress)
    ? ` ${Math.round(Math.min(1, Math.max(0, job.progress)) * 100)}%`
    : ''
  return `${stage}${job.status === 'queued' ? '排队中' : '进行中'}${percent}`
}

function onToggle() {
  setOpen(!open.value)
}

function onDocumentClick(event: MouseEvent) {
  if (!open.value) return
  const root = rootEl.value
  if (root && !root.contains(event.target as Node)) setOpen(false)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) setOpen(false)
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="rootEl" style="position:relative">
    <button class="task-btn" type="button" aria-haspopup="true" :aria-expanded="open" @click="onToggle">
      <svg class="ic sm" viewBox="0 0 24 24"><path d="M22 12h-4l-3 8-4-16-3 8H2" /></svg>
      任务<span v-if="badge" class="b">{{ badge }}</span>
    </button>
    <div v-if="open" class="task-pop" role="dialog" aria-label="任务中心">
      <div class="h">正在运行 · {{ list.length }}</div>
      <template v-if="list.length">
        <div v-for="job in list" :key="job.job_id" class="task-row">
          <span class="task-ic">
            <svg v-if="kindIcon(job) === 'synth'" class="ic sm" viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /></svg>
            <svg v-else-if="kindIcon(job) === 'train'" class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
            <svg v-else class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
          </span>
          <span class="task-t">
            <b>{{ job.title }}</b>
            <span class="d">{{ job.error || progressText(job) }}</span>
            <span v-if="typeof job.progress === 'number' && job.status === 'running'" class="task-bar"><span :style="{ width: `${Math.round(Math.min(1, Math.max(0, job.progress)) * 100)}%` }" /></span>
          </span>
          <button v-if="job.cancelable && job.status !== 'done' && job.status !== 'failed'" class="task-x" type="button" @click="cancel(job)">取消</button>
        </div>
      </template>
      <div v-else class="task-empty">
        {{ loadFailed ? '暂时读不到任务状态。' : '当前没有进行中的任务。' }}
      </div>
    </div>
  </div>
</template>
