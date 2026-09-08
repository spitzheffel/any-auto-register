import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export const API = import.meta.env.VITE_API_BASE || '/api'
export const API_BASE = API

export function getAuthToken(): string {
  return localStorage.getItem('_auth_token') || ''
}
export function setAuthToken(token: string) {
  if (token) localStorage.setItem('_auth_token', token)
  else localStorage.removeItem('_auth_token')
}

function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * 后端错误响应。FastAPI 的 `detail` 可能是字符串，也可能是结构化对象
 * （如 `{ message, missing_channels }`），原样挂在 `detail` 上供调用方细分处理。
 */
export class ApiError extends Error {
  status: number
  detail: any

  constructor(message: string, status: number, detail: any) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** 把响应体转成可读的错误：结构化 detail 只取 message，别把整段 JSON 摊到界面上。 */
async function toApiError(res: Response): Promise<ApiError> {
  const text = await res.text()
  let detail: any = text
  try {
    const parsed = JSON.parse(text)
    detail = parsed?.detail ?? parsed
  } catch {
    // 非 JSON 响应（网关错误页等），直接用原文
  }
  let message = ''
  if (typeof detail === 'string') {
    message = detail
  } else if (Array.isArray(detail)) {
    // FastAPI 422 校验错误是 [{loc, msg, type}] 列表
    message = detail.map((item: any) => item?.msg || JSON.stringify(item)).join('；')
  } else if (typeof detail?.message === 'string') {
    message = detail.message
  }
  return new ApiError(message || text || `请求失败 (${res.status})`, res.status, detail)
}

export async function apiFetch(path: string, opts?: RequestInit) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(opts?.headers || {}) },
  })
  if (res.status === 401 && !path.startsWith('/auth/')) {
    setAuthToken('')
    window.location.reload()
    throw new Error('Unauthorized')
  }
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

export async function apiDownload(path: string, opts?: RequestInit) {
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      ...(opts?.body ? { 'Content-Type': 'application/json' } : {}),
      ...authHeaders(),
      ...(opts?.headers || {}),
    },
  })
  if (res.status === 401) {
    setAuthToken('')
    window.location.reload()
    throw new Error('Unauthorized')
  }
  if (!res.ok) throw await toApiError(res)
  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/)
  const filename = decodeURIComponent(match?.[1] || match?.[2] || 'download')
  return { blob, filename }
}

/** 复制文本。部分内嵌浏览器没有 clipboard 权限，降级到 execCommand。 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走降级分支
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}

export function triggerBrowserDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}
