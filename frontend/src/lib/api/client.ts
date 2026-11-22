/**
 * 请求核心：超时控制、错误归一化与四动词封装。
 *
 * 所有端点模块都经由 `api` / `request` 发请求；页面只依赖这里的
 * `ApiError` 与 `errorMessage`，不直接碰 fetch。
 */

/** A failed API call, carrying the HTTP status so callers can branch on it. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** 404 from a job-status route means the job is gone, not that the call failed. */
  get isMissing(): boolean {
    return this.status === 404
  }
}

/** Abort a request that the backend never answers, so spinners can end. */
const DEFAULT_TIMEOUT_MS = 30_000

/**
 * Uploading a recording or a video is bounded by the user's disk and by how long
 * they are willing to wait — not by the API's response time.
 *
 * The 30 s default is right for a JSON round trip and wrong for a large file:
 * on a slow disk the abort would fire mid-upload, which is a regression against
 * the pre-timeout behaviour where the request could take as long as it needed.
 * Every endpoint that streams a file body uses this instead.
 */
export const UPLOAD_TIMEOUT_MS = 10 * 60_000

export type RequestOptions = {
  method?: string
  body?: BodyInit | null
  /** Override for slow endpoints (uploads, model scans). */
  timeoutMs?: number
  /** Keep the request alive across a page unload (used by the autosave flush). */
  keepalive?: boolean
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const isForm = init.body instanceof FormData
  const method = init.method ?? 'GET'
  const startedAt = performance.now()
  // 调试日志：任何 API 调用都会在控制台留痕（方法/路径/耗时/状态）。
  // 前缀 [xianghua] 方便在控制台里过滤；请求失败或异常也会各留一条。
  console.debug(`[xianghua] → ${method} ${path}`)
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: isForm || !init.body ? undefined : { 'Content-Type': 'application/json' },
    })
    const elapsed = Math.round(performance.now() - startedAt)
    if (!response.ok) {
      console.warn(`[xianghua] ✗ ${method} ${path} → HTTP ${response.status} (${elapsed}ms)`)
    } else {
      console.debug(`[xianghua] ✓ ${method} ${path} → ${response.status} (${elapsed}ms)`)
    }
    if (response.status === 204) return undefined as T
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : ''
      throw new ApiError(detail || `请求失败（HTTP ${response.status}）`, response.status, String(data?.code ?? ''))
    }
    return data as T
  } catch (error) {
    const elapsed = Math.round(performance.now() - startedAt)
    // An abort surfaces as a DOMException; turn it into something the UI can show.
    if (error instanceof DOMException && error.name === 'AbortError') {
      console.warn(`[xianghua] ✗ ${method} ${path} 超时（${timeoutMs}ms）`)
      throw new ApiError('请求超时，请检查后端是否仍在运行。', 0, 'TIMEOUT')
    }
    if (error instanceof TypeError) {
      console.warn(`[xianghua] ✗ ${method} ${path} 连接失败（${elapsed}ms）`)
      throw new ApiError('无法连接后端服务，请确认它已启动。', 0, 'NETWORK')
    }
    console.error(`[xianghua] ✗ ${method} ${path} 异常（${elapsed}ms）`, error)
    throw error
  } finally {
    window.clearTimeout(timer)
  }
}

export const api = {
  get: <T>(path: string, options: RequestOptions = {}) => request<T>(path, options),
  post: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

/** 让调用方判断错误文案时不必重复写 `instanceof`。 */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}
