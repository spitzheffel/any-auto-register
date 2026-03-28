import { useCallback, useEffect, useState } from 'react'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { TaskDetailModal } from '@/components/tasks/TaskDetailModal'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { getTaskStatusText, getTaskTypeText, isActiveTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Activity, CheckCircle2, AlertTriangle, Clock3 } from 'lucide-react'

export default function TaskHistory() {
  const [tasks, setTasks] = useState<any[]>([])
  const [platform, setPlatform] = useState('')
  const [status, setStatus] = useState('')
  const [platforms, setPlatforms] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedTask, setSelectedTask] = useState<any | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50' })
      if (platform) params.set('platform', platform)
      if (status) params.set('status', status)
      const data = await apiFetch(`/tasks?${params}`)
      setTasks(data.items || [])
    } finally {
      setLoading(false)
    }
  }, [platform, status])

  useEffect(() => {
    getPlatforms().then(data => setPlatforms(data || [])).catch(() => setPlatforms([]))
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (!tasks.some(task => isActiveTaskStatus(String(task.status || '')))) return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      load().catch(() => {})
    }, 4000)
    return () => window.clearInterval(timer)
  }, [tasks, load])

  const handleTaskChange = useCallback((nextTask: any) => {
    setSelectedTask(nextTask)
    setTasks(prev => {
      const index = prev.findIndex(item => item.id === nextTask.id)
      if (index === -1) return [nextTask, ...prev]
      const next = [...prev]
      next[index] = nextTask
      return next
    })
    load().catch(() => {})
  }, [load])

  const succeeded = tasks.filter(task => task.status === 'succeeded').length
  const failed = tasks.filter(task => task.status === 'failed').length
  const running = tasks.filter(task => ['running', 'claimed', 'pending', 'cancel_requested'].includes(task.status)).length
  const metricCards = [
    { label: '任务数', value: tasks.length, icon: Activity, tone: 'text-[var(--accent)]' },
    { label: '成功', value: succeeded, icon: CheckCircle2, tone: 'text-emerald-400' },
    { label: '失败', value: failed, icon: AlertTriangle, tone: 'text-red-400' },
    { label: '进行中', value: running, icon: Clock3, tone: 'text-amber-400' },
  ]

  return (
    <div className="space-y-4">
      {selectedTask ? (
        <TaskDetailModal
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onTaskChange={handleTaskChange}
        />
      ) : null}

      <Card className="overflow-hidden p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-semibold text-[var(--text-primary)]">任务记录</div>
            <Badge variant="default">任务 {tasks.length}</Badge>
            <Badge variant="secondary">运行中 {running}</Badge>
          </div>
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label} className="bg-[linear-gradient(180deg,rgba(255,255,255,0.03),rgba(255,255,255,0.01))]">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
              </div>
              <div className="flex h-9 w-9 items-center justify-center rounded-[16px] border border-[var(--border-soft)] bg-[var(--chip-bg)]">
                <Icon className={`h-5 w-5 ${tone}`} />
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Card className="bg-[var(--bg-pane)]/60">
        <div className="space-y-4">
          <div>
            <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">筛选</div>
            <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">按平台和状态回看任务</div>
          </div>
          <div className="grid gap-3 md:grid-cols-[minmax(0,220px)_minmax(0,220px)_1fr]">
            <select
              value={platform}
              onChange={e => setPlatform(e.target.value)}
              className="control-surface appearance-none"
            >
              <option value="">全部平台</option>
              {platforms.map((item: any) => (
                <option key={item.name} value={item.name}>{item.display_name}</option>
              ))}
            </select>
            <select
              value={status}
              onChange={e => setStatus(e.target.value)}
              className="control-surface appearance-none"
            >
              <option value="">全部状态</option>
              <option value="pending">pending</option>
              <option value="claimed">claimed</option>
              <option value="running">running</option>
              <option value="succeeded">succeeded</option>
              <option value="failed">failed</option>
              <option value="interrupted">interrupted</option>
              <option value="cancel_requested">cancel_requested</option>
              <option value="cancelled">cancelled</option>
            </select>
            <div className="toolbar-strip justify-start md:justify-end">
              {platform ? <Badge variant="secondary">{platform}</Badge> : null}
              {status ? <Badge variant="warning">{status}</Badge> : null}
              {!platform && !status ? <Badge variant="secondary">全部任务</Badge> : null}
            </div>
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden p-0">
        <div className="border-b border-[var(--border)] px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
          最近任务
        </div>
        <div className="glass-table-wrap">
        <table className="w-full min-w-[980px] text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
              <th className="px-4 py-2.5 text-left">时间</th>
              <th className="px-4 py-2.5 text-left">任务 ID</th>
              <th className="px-4 py-2.5 text-left">类型</th>
              <th className="px-4 py-2.5 text-left">平台</th>
              <th className="px-4 py-2.5 text-left">状态</th>
              <th className="px-4 py-2.5 text-left">进度</th>
              <th className="px-4 py-2.5 text-left">结果</th>
              <th className="px-4 py-2.5 text-left">错误</th>
              <th className="px-4 py-2.5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-8">
                  <div className="empty-state-panel">当前筛选下没有任务记录。</div>
                </td>
              </tr>
            )}
            {tasks.map(task => (
              <tr key={task.id} className="border-b border-[var(--border)]/40 hover:bg-[var(--bg-hover)]/70">
                <td className="px-4 py-2.5 text-xs text-[var(--text-muted)]">
                  {task.created_at ? new Date(task.created_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
                </td>
                <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-secondary)]">{task.id}</td>
                <td className="px-4 py-2.5 text-xs text-[var(--text-secondary)]">{getTaskTypeText(task.type)}</td>
                <td className="px-4 py-2.5">
                  <Badge variant="secondary">{task.platform || '-'}</Badge>
                </td>
                <td className="px-4 py-2.5">
                  <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>
                    {getTaskStatusText(task.status)}
                  </Badge>
                </td>
                <td className="px-4 py-2.5 text-[var(--text-secondary)]">
                  <span className="rounded-full border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2.5 py-1 text-xs">
                    {task.progress || '-'}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-xs text-[var(--text-secondary)]">
                  成功 {task.success || 0} / 失败 {task.error_count || 0}
                </td>
                <td className="px-4 py-2.5 text-xs">
                  <span className={task.error ? 'text-red-400' : 'text-[var(--text-muted)]'}>{task.error || '-'}</span>
                </td>
                <td className="px-4 py-2.5 text-right">
                  <Button size="sm" variant="outline" onClick={() => setSelectedTask(task)}>
                    查看日志
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </Card>
    </div>
  )
}
