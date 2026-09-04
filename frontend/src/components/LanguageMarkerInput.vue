<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'

type LanguageOption = { value: 'zh' | 'yue' | 'en' | 'ja' | 'ko'; label: string }

const props = defineProps<{
  disabled?: boolean
  languageOptions: LanguageOption[]
  modelValue: string
  placeholder?: string
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const input = ref<HTMLTextAreaElement | null>(null)
const query = ref('')
const suggestionIndex = ref(0)
const suggestionsOpen = ref(false)
let suppressNextKeyup = false

const suggestions = computed(() => {
  const prefix = query.value.toLowerCase()
  return props.languageOptions
    .filter(option => option.value.startsWith(prefix))
    .map((option, index) => ({ ...option, shortcut: String(index + 1) }))
})

function closeSuggestions() {
  suggestionsOpen.value = false
  suggestionIndex.value = 0
}

function isInsideCompleteLanguageTag(value: string, caret: number) {
  const start = value.lastIndexOf('<', Math.max(0, caret - 1))
  const end = value.indexOf('>', caret)
  if (start < 0 || end < 0 || end < start) return false
  return /^<\/?(?:zh|yue|en|ja|ko)>$/i.test(value.slice(start, end + 1))
}

function refreshSuggestions(value = props.modelValue) {
  if (props.disabled) {
    closeSuggestions()
    return
  }
  const caret = input.value?.selectionStart ?? props.modelValue.length
  const match = value.slice(0, caret).match(/<([A-Za-z]*)$/)
  if (!match) {
    closeSuggestions()
    return
  }
  if (isInsideCompleteLanguageTag(value, caret)) {
    closeSuggestions()
    return
  }
  query.value = match[1]
  suggestionIndex.value = 0
  suggestionsOpen.value = suggestions.value.length > 0
}

function selectSuggestion(option: LanguageOption) {
  const element = input.value
  const caret = element?.selectionStart ?? props.modelValue.length
  const before = props.modelValue.slice(0, caret)
  const partialMarker = before.match(/<[^<>]*$/)
  if (!partialMarker) {
    closeSuggestions()
    return
  }

  const start = caret - partialMarker[0].length
  const marker = `<${option.value}></${option.value}>`
  const nextValue = `${props.modelValue.slice(0, start)}${marker}${props.modelValue.slice(caret)}`
  const nextCaret = start + option.value.length + 2

  emit('update:modelValue', nextValue)
  closeSuggestions()
  void nextTick(() => {
    element?.focus()
    element?.setSelectionRange(nextCaret, nextCaret)
  })
}

function handleInput(event: Event) {
  const value = (event.target as HTMLTextAreaElement).value
  emit('update:modelValue', value)
  refreshSuggestions(value)
}

function handleKeydown(event: KeyboardEvent) {
  if (!suggestionsOpen.value || !suggestions.value.length) return

  if (event.key === 'ArrowDown') {
    event.preventDefault()
    suppressNextKeyup = true
    suggestionIndex.value = (suggestionIndex.value + 1) % suggestions.value.length
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    suppressNextKeyup = true
    suggestionIndex.value = (suggestionIndex.value - 1 + suggestions.value.length) % suggestions.value.length
    return
  }
  if (event.key === 'Escape') {
    event.preventDefault()
    suppressNextKeyup = true
    closeSuggestions()
    return
  }

  const numberKey = Number(event.key)
  if (Number.isInteger(numberKey) && numberKey >= 1 && numberKey <= suggestions.value.length) {
    event.preventDefault()
    suppressNextKeyup = true
    selectSuggestion(suggestions.value[numberKey - 1])
    return
  }

  if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault()
    suppressNextKeyup = true
    selectSuggestion(suggestions.value[suggestionIndex.value])
  }
}

function handleKeyup(event: KeyboardEvent) {
  if (suppressNextKeyup) {
    suppressNextKeyup = false
    return
  }
  refreshSuggestions()
}
</script>

<template>
  <div class="language-marker-field">
    <textarea
      ref="input"
      :value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      @blur="closeSuggestions"
      @click="refreshSuggestions()"
      @focus="refreshSuggestions()"
      @input="handleInput"
      @keydown="handleKeydown"
      @keyup="handleKeyup"
    />
    <ul v-if="suggestionsOpen" class="language-marker-menu" role="listbox" aria-label="显式语言标记">
      <li v-for="(option, index) in suggestions" :key="option.value">
        <button
          type="button"
          role="option"
          :aria-selected="index === suggestionIndex"
          :class="{ active: index === suggestionIndex }"
          @mousedown.prevent
          @click="selectSuggestion(option)"
        >
          <b>{{ option.shortcut }}</b>
          <span>{{ option.label }}</span>
          <code>&lt;{{ option.value }}&gt;</code>
        </button>
      </li>
    </ul>
  </div>
</template>
