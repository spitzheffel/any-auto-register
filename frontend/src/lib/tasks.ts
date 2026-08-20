export const TASK_STATUS_VARIANTS: Record<string, any> = {
  pending: 'secondary',
  claimed: 'secondary',
  running: 'default',
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
    case 'claimed':
      return '已领取'
    case 'pending':
      return '排队中'
    default:
      return status
  }
}
