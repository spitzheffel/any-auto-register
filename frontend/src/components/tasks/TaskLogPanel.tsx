import { useEffect, useRef, useState } from 'react'

import { API_BASE, apiFetch } from '@/lib/utils'
import { getTaskStatusText, isTerminalTaskStatus } from '@/lib/tasks'
import { Button } from '@/components/ui/button'

export function TaskLogPanel({
  taskId,
  onDone,
}: {
  taskId: string
  onDone: (status: string) => void
}) {
  const [lines, setLines] = useState<string[]>([])
  const [doneStatus, setDoneStatus] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const seenEventIdsRef = useRef<Set<number>>(new Set())
  const cursorRef = useRef(0)
  const doneRef = useRef(false)
  const onDoneRef = useRef(onDone)
  const sseHealthyRef = useRef(false)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (!taskId) return
    seenEventIdsRef.current = new Set()
    cursorRef.current = 0
    doneRef.current = false
    sseHealthyRef.current = false
    setLines([])
    setDoneStatus(null)

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0)
      if (eventId && seenEventIdsRef.current.has(eventId)) return
      if (eventId) {
        seenEventIdsRef.current.add(eventId)
        cursorRef.current = Math.max(cursorRef.current, eventId)
      }
      if (payload?.line) {
        setLines(prev => [...prev, payload.line])
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

    const poll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return
      try {
        const data = await apiFetch(`/tasks/${taskId}/events?since=${cursorRef.current}`)
        for (const item of data.items || []) {
          pushEvent(item)
        }
        const task = await apiFetch(`/tasks/${taskId}`)
        if (isTerminalTaskStatus(task.status) && !doneRef.current) {
          pushEvent({ done: true, status: task.status })
        }
      } catch {
        // passive
      }
    }, 1000)

    return () => {
      sseHealthyRef.current = false
      eventSourceRef.current?.close()
      eventSourceRef.current = null
      window.clearInterval(poll)
    }
  }, [taskId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [lines])

  return (
    <div className="flex flex-col h-full">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">日志</div>
          <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">任务执行日志</div>
          <div className="mt-1 font-mono text-[11px] text-[var(--text-muted)] break-all">{taskId}</div>
        </div>
        <div className="flex items-center gap-2">
          <div className="rounded-full border border-[var(--border-soft)] bg-[var(--chip-bg)] px-3 py-1 text-xs text-[var(--text-secondary)]">
            {doneStatus ? getTaskStatusText(doneStatus) : '进行中'}
          </div>
          <Button variant="outline" size="sm" asChild>
            <a href="/history">任务记录</a>
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto rounded-[22px] border border-[var(--border)] bg-[linear-gradient(180deg,rgba(3,8,8,0.45),rgba(3,8,8,0.24))] p-4 font-mono text-xs space-y-1 min-h-[220px] max-h-[420px] shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
        {lines.length === 0 && <div className="text-[var(--text-muted)]">等待日志...</div>}
        {lines.map((line, index) => (
          <div
            key={index}
            className={`leading-5 rounded-xl px-2.5 py-1 ${
              line.includes('✓') || line.includes('成功') ? 'text-emerald-400' :
              line.includes('✗') || line.includes('失败') || line.includes('错误') ? 'text-red-400' :
              'text-[var(--text-secondary)]'
            }`}
          >
            {line}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      {doneStatus && (
        <div className={`mt-3 inline-flex w-fit rounded-full border px-3 py-1 text-xs ${
          doneStatus === 'succeeded' ? 'text-emerald-400' :
          doneStatus === 'interrupted' || doneStatus === 'cancelled' ? 'text-amber-400' :
          'text-red-400'
        }`}>
          {getTaskStatusText(doneStatus)}
        </div>
      )}
    </div>
  )
}
