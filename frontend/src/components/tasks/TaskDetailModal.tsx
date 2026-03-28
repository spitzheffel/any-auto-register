import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, RotateCcw, Square, X } from 'lucide-react'

import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { getTaskStatusText, getTaskTypeText, canRetryTask, isActiveTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { apiFetch } from '@/lib/utils'

function formatTime(value: string | null | undefined) {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export function TaskDetailModal({
  task,
  onClose,
  onTaskChange,
}: {
  task: any
  onClose: () => void
  onTaskChange?: (task: any) => void
}) {
  const [currentTask, setCurrentTask] = useState<any>(task)
  const [refreshing, setRefreshing] = useState(false)
  const [interrupting, setInterrupting] = useState(false)
  const [retrying, setRetrying] = useState(false)

  const applyTask = useCallback((nextTask: any) => {
    setCurrentTask(nextTask)
    onTaskChange?.(nextTask)
  }, [onTaskChange])

  const refreshTask = useCallback(async (withLoading = true) => {
    if (!currentTask?.id) return null
    if (withLoading) setRefreshing(true)
    try {
      const latest = await apiFetch(`/tasks/${currentTask.id}`)
      applyTask(latest)
      return latest
    } finally {
      if (withLoading) setRefreshing(false)
    }
  }, [applyTask, currentTask?.id])

  useEffect(() => {
    setCurrentTask(task)
  }, [task])

  useEffect(() => {
    if (!currentTask?.id) return
    refreshTask(false).catch(() => {})
  }, [currentTask?.id, refreshTask])

  useEffect(() => {
    if (!currentTask?.id || !isActiveTaskStatus(String(currentTask.status || ''))) return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      refreshTask(false).catch(() => {})
    }, 3000)
    return () => window.clearInterval(timer)
  }, [currentTask?.id, currentTask?.status, refreshTask])

  const interruptTask = async () => {
    if (!currentTask?.id) return
    if (!window.confirm('确认中断这个任务吗？')) return
    setInterrupting(true)
    try {
      const latest = await apiFetch(`/tasks/${currentTask.id}/cancel`, { method: 'POST' })
      applyTask(latest)
    } catch (error: any) {
      window.alert(error?.message || '中断任务失败')
    } finally {
      setInterrupting(false)
    }
  }

  const retryTask = async () => {
    if (!currentTask?.id) return
    setRetrying(true)
    try {
      const latest = await apiFetch(`/tasks/${currentTask.id}/retry`, { method: 'POST' })
      applyTask(latest)
    } catch (error: any) {
      window.alert(error?.message || '重试任务失败')
    } finally {
      setRetrying(false)
    }
  }

  if (!currentTask) return null

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg flex flex-col"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '88vh' }}
      >
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-[var(--border)]">
          <div className="min-w-0">
            <div className="text-base font-semibold text-[var(--text-primary)]">任务详情</div>
            <div className="mt-1 font-mono text-[11px] text-[var(--text-muted)] break-all">{currentTask.id}</div>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={TASK_STATUS_VARIANTS[currentTask.status] || 'secondary'}>
              {getTaskStatusText(currentTask.status)}
            </Badge>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="px-6 py-4 flex-1 overflow-y-auto space-y-4">
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={() => refreshTask()} disabled={refreshing}>
              <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
              刷新
            </Button>
            {isActiveTaskStatus(String(currentTask.status || '')) ? (
              <Button variant="destructive" size="sm" onClick={interruptTask} disabled={interrupting}>
                <Square className="mr-1.5 h-3.5 w-3.5" />
                {interrupting ? '中断中...' : '中断任务'}
              </Button>
            ) : null}
            {canRetryTask(currentTask) ? (
              <Button variant="outline" size="sm" onClick={retryTask} disabled={retrying}>
                <RotateCcw className={`mr-1.5 h-3.5 w-3.5 ${retrying ? 'animate-spin' : ''}`} />
                {retrying ? '重试中...' : '重试任务'}
              </Button>
            ) : null}
          </div>

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">类型</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{getTaskTypeText(currentTask.type)}</div>
            </div>
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">平台</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{currentTask.platform || '-'}</div>
            </div>
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">进度</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{currentTask.progress || '0/0'}</div>
            </div>
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">结果</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">成功 {currentTask.success || 0} / 失败 {currentTask.error_count || 0}</div>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3 text-xs text-[var(--text-secondary)]">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">创建时间</div>
              <div className="mt-1 text-[var(--text-primary)]">{formatTime(currentTask.created_at)}</div>
            </div>
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3 text-xs text-[var(--text-secondary)]">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">开始时间</div>
              <div className="mt-1 text-[var(--text-primary)]">{formatTime(currentTask.started_at)}</div>
            </div>
            <div className="rounded-[18px] border border-[var(--border)] bg-[var(--bg-pane)]/55 px-4 py-3 text-xs text-[var(--text-secondary)]">
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">结束时间</div>
              <div className="mt-1 text-[var(--text-primary)]">{formatTime(currentTask.finished_at)}</div>
            </div>
          </div>

          {currentTask.error ? (
            <div className="rounded-[18px] border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
              {currentTask.error}
            </div>
          ) : null}

          <TaskLogPanel
            taskId={currentTask.id}
            onDone={() => {
              refreshTask(false).catch(() => {})
            }}
          />
        </div>
      </div>
    </div>
  )
}
