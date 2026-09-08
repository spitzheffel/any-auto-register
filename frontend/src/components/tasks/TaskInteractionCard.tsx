import { useCallback, useEffect, useRef, useState } from 'react'
import { HandHelping } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'

type Interaction = {
  id: number
  kind: string
  prompt: string
  step: string
  schema: { fields?: string[]; options?: string[] }
  context: { page_url?: string; screenshot_path?: string }
  expires_at: string | null
}

const POLL_INTERVAL_MS = 2000

/** 任务挂起等待人工时出现：确认 / 填值 / 选择处理方式。 */
export function TaskInteractionCard({
  taskId,
  active,
  onPending,
  onAnswered,
}: {
  taskId: string
  active: boolean
  /** 轮询到一条新的待回答交互时触发一次，父级可借此刷新任务状态 */
  onPending?: (interaction: Interaction) => void
  onAnswered?: () => void
}) {
  const [pending, setPending] = useState<Interaction | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const onPendingRef = useRef(onPending)
  const notifiedIdRef = useRef<number | null>(null)

  useEffect(() => {
    onPendingRef.current = onPending
  }, [onPending])

  const load = useCallback(async () => {
    try {
      const data = await apiFetch(`/tasks/${taskId}/interactions?status=pending`)
      const next: Interaction | null = (data?.items || [])[0] || null
      setPending(prev => (prev?.id === next?.id ? prev : next))
      // 同一条交互只通知一次，2s 一轮的轮询不能把父级刷成轮询
      if (next && next.id !== notifiedIdRef.current) {
        notifiedIdRef.current = next.id
        onPendingRef.current?.(next)
      } else if (!next) {
        notifiedIdRef.current = null
      }
    } catch {
      // 轮询失败不打断日志面板
    }
  }, [taskId])

  useEffect(() => {
    if (!active) {
      setPending(null)
      return
    }
    load()
    const timer = setInterval(load, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [active, load])

  useEffect(() => {
    setValues({})
    setError('')
  }, [pending?.id])

  if (!pending) return null

  const fields = pending.schema?.fields || []
  const options = pending.schema?.options || []

  const submit = async (payload: Record<string, unknown>) => {
    setSubmitting(true)
    setError('')
    try {
      await apiFetch(`/tasks/${taskId}/interactions/${pending.id}/respond`, {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      setPending(null)
      onAnswered?.()
    } catch (err: any) {
      setError(err?.message || '提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  const submitInput = () => {
    if (fields.length > 1) {
      submit({ values })
    } else {
      submit({ value: values[fields[0] || 'value'] || '' })
    }
  }

  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3.5 py-3 space-y-3">
      <div className="flex items-start gap-2 text-amber-300">
        <HandHelping className="mt-0.5 h-4 w-4 flex-shrink-0" />
        <div className="min-w-0">
          <p className="text-sm font-medium leading-5">{pending.prompt}</p>
          {pending.step ? (
            <p className="mt-0.5 text-xs text-amber-400/80">步骤: {pending.step}</p>
          ) : null}
          {pending.context?.page_url ? (
            <p className="mt-0.5 truncate font-mono text-xs text-amber-400/70">
              {pending.context.page_url}
            </p>
          ) : null}
        </div>
      </div>

      {pending.context?.screenshot_path ? (
        <img
          src={pending.context.screenshot_path}
          alt="页面截图"
          className="max-h-64 w-full rounded border border-[var(--border)] object-contain"
        />
      ) : null}

      {pending.kind === 'choice' ? (
        <div className="flex flex-wrap gap-2">
          {options.map(option => (
            <Button
              key={option}
              size="sm"
              variant="outline"
              disabled={submitting}
              onClick={() => submit({ option })}
            >
              {option}
            </Button>
          ))}
        </div>
      ) : null}

      {pending.kind === 'input' || pending.kind === 'captcha' ? (
        <div className="space-y-2">
          {(fields.length ? fields : ['value']).map(field => (
            <input
              key={field}
              className="w-full rounded border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 text-sm"
              placeholder={field}
              value={values[field] || ''}
              disabled={submitting}
              onChange={e => setValues(prev => ({ ...prev, [field]: e.target.value }))}
              onKeyDown={e => {
                if (e.key === 'Enter') submitInput()
              }}
            />
          ))}
          <Button size="sm" disabled={submitting} onClick={submitInput}>
            {submitting ? '提交中...' : '提交'}
          </Button>
        </div>
      ) : null}

      {pending.kind === 'confirm' ? (
        <Button size="sm" disabled={submitting} onClick={() => submit({ value: 'confirmed' })}>
          {submitting ? '提交中...' : '我已完成，继续'}
        </Button>
      ) : null}

      {error ? <p className="text-xs text-red-400">{error}</p> : null}
    </div>
  )
}
