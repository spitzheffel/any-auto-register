import { useEffect, useState } from 'react'
import { X, OctagonX } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { apiFetch } from '@/lib/utils'
import {
  getTaskStatusText,
  getTaskTypeText,
  isCancellableTaskStatus,
  TASK_STATUS_VARIANTS,
} from '@/lib/tasks'

export function TaskDetailModal({
  task,
  onClose,
  onStatusChange,
}: {
  task: any
  onClose: () => void
  onStatusChange?: (status: string) => void
}) {
  const [status, setStatus] = useState<string>(task.status)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState('')

  useEffect(() => {
    setStatus(task.status)
  }, [task.id, task.status])

  const updateStatus = (next: string) => {
    setStatus(next)
    onStatusChange?.(next)
  }

  const cancel = async () => {
    setCancelling(true)
    setCancelError('')
    try {
      const updated = await apiFetch(`/tasks/${task.id}/cancel`, { method: 'POST' })
      updateStatus(updated?.status || 'cancel_requested')
    } catch (err: any) {
      setCancelError(err?.message || '终止任务失败')
    } finally {
      setCancelling(false)
    }
  }

  const cancellable = isCancellableTaskStatus(status)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex-shrink-0 flex items-center justify-between gap-4 px-6 py-4 border-b border-[var(--border)]">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-[var(--text-primary)]">
              {getTaskTypeText(task.type)}
              {task.platform ? ` · ${task.platform}` : ''}
            </h2>
            <p className="mt-0.5 truncate font-mono text-xs text-[var(--text-muted)]">{task.id}</p>
          </div>
          <div className="flex flex-shrink-0 items-center gap-3">
            <Badge variant={TASK_STATUS_VARIANTS[status] || 'secondary'}>
              {getTaskStatusText(status)}
            </Badge>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
          {cancelError ? (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-2.5 text-xs leading-5 text-red-400">
              {cancelError}
            </div>
          ) : null}
          {status === 'cancel_requested' ? (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3.5 py-2.5 text-xs leading-5 text-amber-400">
              已发出终止请求。任务会在当前这一步执行完后停下，浏览器注册等待验证码时最长可能还需要等一到两分钟。
            </div>
          ) : null}
          <TaskLogPanel taskId={task.id} onDone={updateStatus} />
        </div>

        <div className="flex-shrink-0 flex justify-end gap-3 px-6 py-4 border-t border-[var(--border)]">
          {cancellable ? (
            <Button variant="destructive" onClick={cancel} disabled={cancelling} className="flex-1">
              <OctagonX className="mr-2 h-4 w-4" />
              {cancelling ? '终止中...' : '终止任务'}
            </Button>
          ) : null}
          <Button variant="outline" onClick={onClose} className={cancellable ? 'flex-1' : ''}>
            关闭
          </Button>
        </div>
      </div>
    </div>
  )
}
