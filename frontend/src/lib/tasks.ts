import { apiFetch } from '@/lib/utils'

export const TASK_STATUS_VARIANTS: Record<string, any> = {
  pending: 'secondary',
  claimed: 'secondary',
  running: 'default',
  waiting_human: 'warning',
  succeeded: 'success',
  failed: 'danger',
  interrupted: 'warning',
  cancel_requested: 'warning',
  cancelled: 'warning',
}

export const TERMINAL_TASK_STATUSES = new Set([
  'succeeded',
  'failed',
  'interrupted',
  'cancelled',
])

export function isTerminalTaskStatus(status: string) {
  return TERMINAL_TASK_STATUSES.has(status)
}

export const CANCELLABLE_TASK_STATUSES = new Set([
  'pending',
  'claimed',
  'running',
  'waiting_human',
  'cancel_requested',
])

export function isCancellableTaskStatus(status: string) {
  return CANCELLABLE_TASK_STATUSES.has(status)
}

export function getTaskTypeText(type: string) {
  switch (type) {
    case 'register':
      return '批量注册'
    case 'account_check':
      return '账号检测'
    case 'account_check_all':
      return '批量检测'
    case 'platform_action':
      return '平台操作'
    default:
      return type || '任务'
  }
}

// ---------------------------------------------------------------------------
// 失败现场
// ---------------------------------------------------------------------------

export type AttemptArtifactBundle = {
  /** 现场包目录名，形如 <step>-<retry> */
  name: string
  files: string[]
}

export const ARTIFACT_FILE_LABELS: Record<string, string> = {
  'screenshot.png': '截图',
  'page.html.gz': '页面 HTML',
  'network.json': '网络记录',
  'console.json': '控制台',
  'meta.json': '元信息',
}

export function getArtifactFileLabel(filename: string) {
  return ARTIFACT_FILE_LABELS[filename] || filename
}

/** 列出某次尝试落盘的全部失败现场包，没有现场时为空数组。 */
export async function listAttemptArtifacts(
  taskId: string,
  attemptId: string,
): Promise<AttemptArtifactBundle[]> {
  const data = await apiFetch(
    `/tasks/${encodeURIComponent(taskId)}/attempts/${encodeURIComponent(attemptId)}/artifacts`,
  )
  return (data?.items || []).map((item: any) => ({
    name: String(item?.name || ''),
    files: (item?.files || []).map(String),
  }))
}

/** 现场包单文件的 API 路径，配合 apiFetch / apiDownload 使用（会带上鉴权头）。 */
export function attemptArtifactPath(
  taskId: string,
  attemptId: string,
  bundle: string,
  filename: string,
) {
  return (
    `/tasks/${encodeURIComponent(taskId)}/attempts/${encodeURIComponent(attemptId)}` +
    `/artifacts/${encodeURIComponent(bundle)}/${encodeURIComponent(filename)}`
  )
}

/** step_failed 事件的 detail.artifacts 是落盘目录绝对路径，末段即现场包名。 */
export function getArtifactBundleName(detail: any): string {
  const directory = String(detail?.artifacts || '')
  if (directory) {
    const segment = directory.split(/[\\/]/).filter(Boolean).pop()
    if (segment) return segment
  }
  const step = String(detail?.step || '')
  const attempt = Number(detail?.attempt || 0)
  return step && attempt ? `${step}-${attempt}` : ''
}

export function getTaskStatusText(status: string) {
  switch (status) {
    case 'succeeded':
      return '已完成'
    case 'failed':
      return '失败'
    case 'interrupted':
      return '已中断'
    case 'cancelled':
      return '已取消'
    case 'cancel_requested':
      return '取消中'
    case 'running':
      return '执行中'
    case 'waiting_human':
      return '等待人工'
    case 'claimed':
      return '已领取'
    case 'pending':
      return '排队中'
    default:
      return status
  }
}
