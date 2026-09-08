import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { TaskFailureArtifacts } from '@/components/tasks/TaskFailureArtifacts'
import { TaskInteractionCard } from '@/components/tasks/TaskInteractionCard'
import { API_BASE, apiFetch, copyToClipboard } from '@/lib/utils'
import {
  getArtifactBundleName,
  getTaskStatusText,
  isTerminalTaskStatus,
  listAttemptArtifacts,
} from '@/lib/tasks'

type LogEvent = {
  line: string
  type: string
  detail: Record<string, any>
}

/** attempt_id -> 现场包名 -> 文件列表 */
type ArtifactIndex = Record<string, Record<string, string[]>>

function getLineTone(line: string) {
  if (line.includes('✗') || line.includes('失败') || line.includes('错误')) return 'text-red-500'
  if (line.includes('✓') || line.includes('成功')) return 'text-emerald-500'
  return 'text-[var(--text-secondary)]'
}

export function TaskLogPanel({
  taskId,
  onDone,
}: {
  taskId: string
  onDone: (status: string) => void
}) {
  const [events, setEvents] = useState<LogEvent[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactIndex>({})
  const listedAttemptsRef = useRef<Set<string>>(new Set())
  /** 现场列目录失败的 attempt -> 现场包名，下一批事件到来时重试 */
  const retryArtifactsRef = useRef<Map<string, Set<string>>>(new Map())
  /** events 里已经扫过现场引用的条数，新事件只扫尾部 */
  const scannedEventsRef = useRef(0)
  const [task, setTask] = useState<any | null>(null)
  const [doneStatus, setDoneStatus] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const seenEventIdsRef = useRef<Set<number>>(new Set())
  const cursorRef = useRef(0)
  const doneRef = useRef(false)
  const onDoneRef = useRef(onDone)
  const sseHealthyRef = useRef(false)
  const eventSourceRef = useRef<EventSource | null>(null)
  const mountedRef = useRef(true)
  const taskIdRef = useRef(taskId)
  const syncTaskRef = useRef<(() => Promise<void>) | null>(null)

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    if (!taskId) return
    taskIdRef.current = taskId
    seenEventIdsRef.current = new Set()
    cursorRef.current = 0
    doneRef.current = false
    sseHealthyRef.current = false
    listedAttemptsRef.current = new Set()
    retryArtifactsRef.current = new Map()
    setEvents([])
    setArtifacts({})
    setTask(null)
    setDoneStatus(null)

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0)
      if (eventId && seenEventIdsRef.current.has(eventId)) return
      if (eventId) {
        seenEventIdsRef.current.add(eventId)
        cursorRef.current = Math.max(cursorRef.current, eventId)
      }
      if (payload?.line) {
        setEvents(prev => [
          ...prev,
          {
            line: String(payload.line),
            type: String(payload.type || 'log'),
            detail: payload.detail || {},
          },
        ])
      }
      if (payload?.done && !doneRef.current) {
        doneRef.current = true
        sseHealthyRef.current = false
        eventSourceRef.current?.close()
        eventSourceRef.current = null
        const nextStatus = payload.status || 'succeeded'
        setDoneStatus(nextStatus)
        onDoneRef.current(nextStatus)
      }
    }

    // 拉取游标之后的全部历史事件，翻页直到取完
    const drainEvents = async () => {
      const pageSize = 200
      for (let page = 0; page < 20; page += 1) {
        const data = await apiFetch(`/tasks/${taskId}/events?since=${cursorRef.current}&limit=${pageSize}`)
        const items = data.items || []
        for (const item of items) {
          pushEvent(item)
        }
        if (items.length < pageSize) return
      }
    }

    const syncTask = async () => {
      const latest = await apiFetch(`/tasks/${taskId}`)
      setTask(latest)
      if (isTerminalTaskStatus(latest.status) && !doneRef.current) {
        // 已结束的任务要先补齐历史日志，否则收尾会把流关掉、日志一条都看不到
        await drainEvents().catch(() => {})
        pushEvent({ done: true, status: latest.status })
      }
    }
    syncTaskRef.current = syncTask

    const es = new EventSource(`${API_BASE}/tasks/${taskId}/logs/stream`)
    eventSourceRef.current = es
    es.onopen = () => {
      sseHealthyRef.current = true
    }
    es.onmessage = (e) => {
      sseHealthyRef.current = true
      pushEvent(JSON.parse(e.data))
    }
    es.onerror = () => {
      if (doneRef.current) {
        es.close()
        if (eventSourceRef.current === es) {
          eventSourceRef.current = null
        }
        return
      }
      sseHealthyRef.current = false
    }

    syncTask().catch(() => {})

    const poll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return
      try {
        await drainEvents()
        await syncTask()
      } catch {
        // passive
      }
    }, 1000)

    return () => {
      sseHealthyRef.current = false
      syncTaskRef.current = null
      eventSourceRef.current?.close()
      eventSourceRef.current = null
      window.clearInterval(poll)
    }
  }, [taskId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events])

  // 每出现新的现场包引用就列一次该尝试的目录，拿不到就当没有现场。
  // 这个 effect 随每条事件触发，实时流下几乎每次都会在请求返回前被重跑，
  // 所以不能用 cleanup 作废请求——只在组件卸载 / 切任务时丢弃结果。
  useEffect(() => {
    // 切任务时 events 被清空，扫描游标随之归零
    if (events.length < scannedEventsRef.current) scannedEventsRef.current = 0

    const wanted = new Map<string, Set<string>>(retryArtifactsRef.current)
    retryArtifactsRef.current = new Map()
    for (let index = scannedEventsRef.current; index < events.length; index += 1) {
      const event = events[index]
      if (event.type !== 'step_failed') continue
      const attemptId = String(event.detail?.attempt_id || '')
      const bundle = getArtifactBundleName(event.detail)
      if (!attemptId || !bundle) continue
      const names = wanted.get(attemptId) || new Set<string>()
      names.add(bundle)
      wanted.set(attemptId, names)
    }
    scannedEventsRef.current = events.length

    for (const [attemptId, names] of wanted) {
      const key = `${attemptId}|${[...names].sort().join(',')}`
      if (listedAttemptsRef.current.has(key)) continue
      listedAttemptsRef.current.add(key)
      listAttemptArtifacts(taskId, attemptId)
        .then(items => {
          if (!mountedRef.current || taskIdRef.current !== taskId || !items.length) return
          const bundles: Record<string, string[]> = {}
          for (const item of items) bundles[item.name] = item.files
          setArtifacts(prev => ({ ...prev, [attemptId]: bundles }))
        })
        .catch(() => {
          // 网络抖动等失败放开 key，攒进重试表，下一批事件到来时再列一次
          listedAttemptsRef.current.delete(key)
          if (!mountedRef.current || taskIdRef.current !== taskId) return
          const pending = retryArtifactsRef.current.get(attemptId) || new Set<string>()
          for (const name of names) pending.add(name)
          retryArtifactsRef.current.set(attemptId, pending)
        })
    }
  }, [events, taskId])

  // 人工交互卡片发现待回答 / 已回答时，刷一次任务状态，别等到任务结束才知道它在等人
  const refreshTask = useCallback(() => {
    syncTaskRef.current?.().catch(() => {})
  }, [])
  const handleInteractionAnswered = useCallback(() => {
    refreshTask()
    // 回答落库后 runner 线程最多要 1s 才醒来把状态改回 running，稍后再刷一次
    window.setTimeout(refreshTask, 2000)
  }, [refreshTask])

  const currentStatus = doneStatus || task?.status || 'running'
  const progress = task?.progress_detail || {}
  const progressTotal = Number(progress.total || 0)
  const progressCurrent = Number(progress.current || 0)
  const progressPercent = progressTotal > 0 ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100)) : 0
  const errorText = task?.error || (Array.isArray(task?.errors) ? task.errors[0] : '')
  const statusTone =
    currentStatus === 'succeeded' ? 'text-emerald-500' :
    currentStatus === 'failed' ? 'text-red-500' :
    currentStatus === 'cancelled' || currentStatus === 'interrupted' ? 'text-amber-500' :
    'text-amber-500'
  const progressTone =
    currentStatus === 'failed' ? 'bg-red-500' :
    currentStatus === 'succeeded' ? 'bg-emerald-500' :
    'bg-[var(--accent)]'

  const copyLogs = () => {
    copyToClipboard(events.map(event => event.line).join('\n')).catch(() => {})
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="grid gap-3 grid-cols-3">
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] text-[var(--text-muted)]">状态</div>
          <div className={`mt-1 text-sm font-semibold ${statusTone}`}>{getTaskStatusText(currentStatus)}</div>
        </div>
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] text-[var(--text-muted)]">进度</div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{progress.label || task?.progress || '0/0'}</div>
        </div>
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] text-[var(--text-muted)]">日志</div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{events.length} 条</div>
        </div>
      </div>

      <div className="h-1.5 overflow-hidden rounded-full bg-[var(--chip-bg)]">
        <div
          className={`h-full rounded-full ${progressTone}`}
          style={{ width: `${progressTotal > 0 ? progressPercent : (isTerminalTaskStatus(currentStatus) ? 100 : 18)}%` }}
        />
      </div>

      {errorText ? (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-2.5 text-xs leading-5 text-red-400">
          <div className="mb-1 font-medium">失败原因</div>
          <div className="break-words">{errorText}</div>
        </div>
      ) : null}

      <TaskInteractionCard
        taskId={taskId}
        active={!isTerminalTaskStatus(currentStatus)}
        onPending={refreshTask}
        onAnswered={handleInteractionAnswered}
      />

      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium text-[var(--text-primary)]">执行日志</div>
        <Button type="button" variant="outline" size="sm" onClick={copyLogs} disabled={events.length === 0}>
          复制日志
        </Button>
      </div>

      <div className="min-h-[260px] flex-1 overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-input)] p-3 font-mono text-xs leading-5">
        {events.length === 0 ? (
          <div className="flex h-full min-h-[180px] items-center justify-center text-[var(--text-muted)]">
            等待任务日志...
          </div>
        ) : (
          <div className="space-y-1">
            {events.map((event, index) => {
              const failure = event.type === 'step_failed' ? event.detail : null
              const attemptId = String(failure?.attempt_id || '')
              const bundle = failure ? getArtifactBundleName(failure) : ''
              const files = (bundle && artifacts[attemptId]?.[bundle]) || []
              const failureError = String(failure?.error || '')
              return (
                <div key={index}>
                  <div className={getLineTone(event.line)}>{event.line}</div>
                  {files.length ? (
                    <TaskFailureArtifacts
                      taskId={taskId}
                      attemptId={attemptId}
                      bundle={bundle}
                      files={files}
                      category={String(failure?.category || '')}
                      // 失败这一行本身通常已经带上了错误文案，避免重复
                      error={failureError && !event.line.includes(failureError) ? failureError : ''}
                    />
                  ) : null}
                </div>
              )
            })}
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
