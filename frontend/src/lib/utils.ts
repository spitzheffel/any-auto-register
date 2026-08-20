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
  if (!res.ok) throw new Error(await res.text())
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
  if (!res.ok) throw new Error(await res.text())
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
