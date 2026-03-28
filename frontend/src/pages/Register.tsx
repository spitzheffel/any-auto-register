import { useCallback, useEffect, useRef, useState } from 'react'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse, ProviderOption, ProviderSetting } from '@/lib/config-options'
import { getCaptchaStrategyLabel, getProviderSelectOptions, listProviderFieldKeys } from '@/lib/config-options'
import { apiFetch } from '@/lib/utils'
import { buildExecutorOptions, buildRegistrationOptions, hasReusableOAuthBrowser, pickOAuthExecutor } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Button } from '@/components/ui/button'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Play, CheckCircle, XCircle, Loader2, Orbit, Mail, ScanText, ShieldCheck, Workflow, RotateCcw, Square } from 'lucide-react'
import { canRetryTask, getTaskStatusText, isActiveTaskStatus, isTerminalTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'

const FALLBACK_PLATFORMS = [
  { name: 'chatgpt', display_name: 'ChatGPT' },
  { name: 'cursor', display_name: 'Cursor' },
  { name: 'grok', display_name: 'Grok' },
  { name: 'kiro', display_name: 'Kiro (AWS Builder ID)' },
  { name: 'openblocklabs', display_name: 'OpenBlockLabs' },
  { name: 'tavily', display_name: 'Tavily' },
  { name: 'trae', display_name: 'Trae.ai' },
]

const DEFAULT_FORM: Record<string, any> = {
  platform: 'trae',
  email: '',
  password: '',
  count: 1,
  proxy: '',
  executor_type: 'protocol',
  captcha_solver: 'auto',
  identity_provider: 'mailbox',
  oauth_provider: '',
  oauth_email_hint: '',
  chrome_user_data_dir: '',
  chrome_cdp_url: '',
  mail_provider: 'moemail',
  solver_url: 'http://localhost:8889',
}

function getProviderSetting(settings: ProviderSetting[] = [], providerKey: string) {
  return settings.find(item => item.provider_key === providerKey) || null
}

function getProviderMergedValues(setting: ProviderSetting | null) {
  return {
    ...(setting?.config || {}),
    ...(setting?.auth || {}),
  }
}

export default function Register() {
  const [form, setForm] = useState<Record<string, any>>(DEFAULT_FORM)
  const [platforms, setPlatforms] = useState<any[]>([])
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
  })
  const [optionsError, setOptionsError] = useState('')
  const [task, setTask] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  const [taskActionLoading, setTaskActionLoading] = useState<'cancel' | 'retry' | null>(null)
  const handledTerminalTaskIdsRef = useRef<Set<string>>(new Set())
  const openedCashierTaskIdsRef = useRef<Set<string>>(new Set())

  const set = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))

  const applyTerminalTask = useCallback((latest: any, statusHint?: string) => {
    setTask(latest)
    const taskKey = String(latest?.task_id || latest?.id || task?.task_id || '')
    if (!taskKey) return
    handledTerminalTaskIdsRef.current.add(taskKey)
    const resolvedStatus = statusHint || latest?.status || ''
    if (
      resolvedStatus === 'succeeded'
      && latest?.cashier_urls
      && latest.cashier_urls.length > 0
      && !openedCashierTaskIdsRef.current.has(taskKey)
    ) {
      openedCashierTaskIdsRef.current.add(taskKey)
      latest.cashier_urls.forEach((url: string) => window.open(url, '_blank'))
    }
  }, [task?.task_id])

  useEffect(() => {
    Promise.all([
      getConfig().catch(() => ({})),
      getPlatforms().catch(() => []),
      getConfigOptions().catch(() => null),
    ]).then(([cfg, ps, options]) => {
      setPlatforms(ps || [])
      if (options) {
        setConfigOptions(options)
        setOptionsError('')
      } else {
        setConfigOptions({ mailbox_providers: [], captcha_providers: [], mailbox_settings: [], captcha_settings: [], captcha_policy: {} })
        setOptionsError('未加载到 provider 元数据。请重启后端后刷新页面。')
      }
      setForm(f => {
        const nextForm: Record<string, any> = {
          ...f,
          executor_type: cfg.default_executor || f.executor_type,
          captcha_solver: 'auto',
          identity_provider: cfg.default_identity_provider || f.identity_provider,
          oauth_provider: cfg.default_oauth_provider || f.oauth_provider,
          oauth_email_hint: cfg.oauth_email_hint || f.oauth_email_hint,
          chrome_user_data_dir: cfg.chrome_user_data_dir || f.chrome_user_data_dir,
          chrome_cdp_url: cfg.chrome_cdp_url || f.chrome_cdp_url,
          mail_provider: cfg.mail_provider || f.mail_provider,
          solver_url: cfg.solver_url || f.solver_url,
        }
        const providerFieldKeys = listProviderFieldKeys([
          ...((options?.mailbox_providers as ProviderOption[]) || []),
          ...((options?.captcha_providers as ProviderOption[]) || []),
        ])
        providerFieldKeys.forEach(fieldKey => {
          nextForm[fieldKey] = cfg[fieldKey] || f[fieldKey] || ''
        })
        return nextForm
      })
    })
  }, [])

  const currentPlatform = platforms.find((p: any) => p.name === form.platform) || null
  const platformOptionsSource = platforms.length > 0 ? platforms : FALLBACK_PLATFORMS
  const platformOptions = platformOptionsSource.map((p: any) => [p.name, p.display_name])
  const supportedExecutors = currentPlatform?.supported_executors || ['protocol']
  const registrationOptions = buildRegistrationOptions(currentPlatform)
  const executorOptions = buildExecutorOptions(form.identity_provider, supportedExecutors, hasReusableOAuthBrowser(form))
  const mailboxProviderOptions = getProviderSelectOptions(configOptions.mailbox_providers || [])
  const currentMailboxProvider = (configOptions.mailbox_providers || []).find(provider => provider.value === form.mail_provider) || null
  const currentMailboxSetting = getProviderSetting(configOptions.mailbox_settings || [], form.mail_provider)
  const allProviderFieldKeys = listProviderFieldKeys([
    ...(configOptions.mailbox_providers || []),
    ...(configOptions.captcha_providers || []),
  ])

  useEffect(() => {
    if (!currentMailboxProvider) return
    const values = getProviderMergedValues(currentMailboxSetting)
    const fields = currentMailboxProvider.fields || []
    if (fields.length === 0) return
    setForm(current => {
      const next = { ...current }
      let changed = false
      fields.forEach(field => {
        const nextValue = values[field.key] ?? current[field.key] ?? ''
        if ((next[field.key] ?? '') !== nextValue) {
          next[field.key] = nextValue
          changed = true
        }
      })
      return changed ? next : current
    })
  }, [form.mail_provider, currentMailboxProvider, currentMailboxSetting])

  useEffect(() => {
    if (!platformOptionsSource.some((p: any) => p.name === form.platform)) {
      const fallback = platformOptionsSource[0]?.name || 'trae'
      if (fallback !== form.platform) {
        set('platform', fallback)
      }
    }
  }, [form.platform, platforms.length])

  useEffect(() => {
    if (registrationOptions.length === 0) return
    const currentRegistration = registrationOptions.find(option =>
      option.identityProvider === form.identity_provider &&
      option.oauthProvider === form.oauth_provider,
    )
    if (!currentRegistration) {
      const preferred = registrationOptions.find(option =>
        option.identityProvider === form.identity_provider,
      ) || registrationOptions[0]
      set('identity_provider', preferred.identityProvider)
      set('oauth_provider', preferred.oauthProvider)
    }
  }, [registrationOptions, form.identity_provider, form.oauth_provider, form.platform])

  useEffect(() => {
    const validExecutors = executorOptions.filter(option => !option.disabled)
    if (validExecutors.length === 0) return
    if (!validExecutors.some(option => option.value === form.executor_type)) {
      const nextExecutor = form.identity_provider === 'oauth_browser'
        ? pickOAuthExecutor(supportedExecutors, form.executor_type, hasReusableOAuthBrowser(form))
        : ((supportedExecutors.includes(form.executor_type) && form.executor_type) ? form.executor_type : supportedExecutors[0] || 'protocol')
      set('executor_type', validExecutors.find(option => option.value === nextExecutor)?.value || validExecutors[0].value)
    }
  }, [executorOptions, supportedExecutors, form.executor_type, form.identity_provider, form.chrome_user_data_dir, form.chrome_cdp_url])

  const submit = async () => {
    const extra: Record<string, any> = {
      mail_provider: form.mail_provider,
      identity_provider: form.identity_provider,
      oauth_provider: form.oauth_provider,
      oauth_email_hint: form.oauth_email_hint,
      chrome_user_data_dir: form.chrome_user_data_dir || undefined,
      chrome_cdp_url: form.chrome_cdp_url || undefined,
    }
    allProviderFieldKeys.forEach(fieldKey => {
      if (form[fieldKey] !== undefined) {
        extra[fieldKey] = form[fieldKey]
      }
    })
    const res = await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify({
        platform: form.platform,
        email: form.email || null,
        password: form.password || null,
        count: form.count,
        proxy: form.proxy || null,
        executor_type: form.executor_type,
        captcha_solver: 'auto',
        extra,
      }),
    })
    setTask(res)
    setPolling(true)
  }

  const handleTaskDone = useCallback(async (status: string) => {
    if (!task?.task_id) return
    if (handledTerminalTaskIdsRef.current.has(String(task.task_id))) {
      setPolling(false)
      return
    }
    try {
      const latest = await apiFetch(`/tasks/${task.task_id}`)
      applyTerminalTask(latest, status)
    } finally {
      setPolling(false)
    }
  }, [applyTerminalTask, task?.task_id])

  const cancelTask = async () => {
    if (!task?.task_id) return
    if (!window.confirm('确认中断当前任务吗？')) return
    setTaskActionLoading('cancel')
    try {
      const latest = await apiFetch(`/tasks/${task.task_id}/cancel`, { method: 'POST' })
      setTask(latest)
      if (isTerminalTaskStatus(latest.status)) {
        setPolling(false)
      }
    } finally {
      setTaskActionLoading(null)
    }
  }

  const retryTask = async () => {
    if (!task?.task_id) return
    setTaskActionLoading('retry')
    try {
      const latest = await apiFetch(`/tasks/${task.task_id}/retry`, { method: 'POST' })
      setTask(latest)
      setPolling(true)
    } finally {
      setTaskActionLoading(null)
    }
  }

  useEffect(() => {
    if (!task?.task_id || isTerminalTaskStatus(task.status)) {
      if (task?.status) {
        setPolling(false)
      }
      return
    }
    const interval = window.setInterval(async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const latest = await apiFetch(`/tasks/${task.task_id}`)
        setTask(latest)
        if (isTerminalTaskStatus(latest.status)) {
          window.clearInterval(interval)
          setPolling(false)
          applyTerminalTask(latest)
        }
      } catch {
        // passive
      }
    }, 5000)
    return () => window.clearInterval(interval)
  }, [applyTerminalTask, task?.task_id, task?.status])

  const Input = ({ label, k, type = 'text', placeholder = '' }: any) => (
    <div>
      <label className="block text-xs text-[var(--text-muted)] mb-1">{label}</label>
      <input
        type={type}
        value={(form as any)[k]}
        onChange={e => set(k, type === 'number' ? Number(e.target.value) : e.target.value)}
        placeholder={placeholder}
        className="control-surface"
      />
    </div>
  )

  const Select = ({ label, k, options }: any) => (
    <div>
      <label className="block text-xs text-[var(--text-muted)] mb-1">{label}</label>
      <select
        value={(form as any)[k]}
        onChange={e => set(k, e.target.value)}
        className="control-surface appearance-none"
      >
        {options.map(([v, l]: any) => <option key={v} value={v}>{l}</option>)}
      </select>
    </div>
  )

  const renderProviderField = (field: any) => (
    <Input
      key={field.key}
      label={field.label}
      k={field.key}
      type={field.secret ? 'password' : 'text'}
      placeholder={field.placeholder || ''}
    />
  )

  const summaryRegistration = registrationOptions.find(option => option.identityProvider === form.identity_provider && option.oauthProvider === form.oauth_provider)?.label || '-'
  const summaryExecutor = executorOptions.find(option => option.value === form.executor_type)?.label || '-'
  const summaryVerification = getCaptchaStrategyLabel(form.executor_type, configOptions.captcha_policy, configOptions.captcha_providers)
  const activeTaskStats = task ? [
    { label: '状态', value: getTaskStatusText(task.status), icon: Orbit },
    { label: '进度', value: task.progress || '0/0', icon: Workflow },
    { label: '成功', value: String(task.success ?? 0), icon: CheckCircle },
    { label: '失败', value: String(task.error_count ?? task.errors?.length ?? 0), icon: XCircle },
  ] : []

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_340px]">
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle>基本配置</CardTitle></CardHeader>
            <CardContent className="space-y-4">
              <Select label="平台" k="platform" options={platformOptions} />
              <div className="grid gap-4 md:grid-cols-2">
                <Input label="批量数量" k="count" type="number" />
                <Input label="代理 (可选)" k="proxy" placeholder="http://user:pass@host:port" />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Step 1 · 注册身份</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 md:grid-cols-2">
                {registrationOptions.map((option) => {
                  const active = form.identity_provider === option.identityProvider && form.oauth_provider === option.oauthProvider
                  return (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => {
                        set('identity_provider', option.identityProvider)
                        set('oauth_provider', option.oauthProvider)
                      }}
                      className={`rounded-[22px] border px-4 py-4 text-left transition-colors ${
                        active
                          ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                          : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                      }`}
                    >
                      <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                        {option.identityProvider === 'mailbox' ? <Mail className="h-4 w-4 text-[var(--accent)]" /> : <ShieldCheck className="h-4 w-4 text-[var(--accent)]" />}
                        {option.label}
                      </div>
                      <div className="mt-2 text-xs leading-5 text-[var(--text-muted)]">{option.description}</div>
                    </button>
                  )
                })}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Step 2 · 执行通道</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 md:grid-cols-3">
                {executorOptions.map((option) => {
                  const active = form.executor_type === option.value
                  return (
                    <button
                      key={option.value}
                      type="button"
                      disabled={option.disabled}
                      onClick={() => !option.disabled && set('executor_type', option.value)}
                      className={`rounded-[22px] border px-4 py-4 text-left transition-colors ${
                        option.disabled
                          ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                          : active
                            ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                            : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                      }`}
                    >
                      <div className="text-sm font-medium text-[var(--text-primary)]">{option.label}</div>
                      <div className="mt-2 text-xs leading-5 text-[var(--text-muted)]">{option.description}</div>
                      {option.reason ? (
                        <div className="mt-2 text-xs text-amber-400">{option.reason}</div>
                      ) : null}
                    </button>
                  )
                })}
              </div>
              {form.identity_provider === 'oauth_browser' && (
                <>
                  <Input label="预期登录邮箱 (可选)" k="oauth_email_hint" placeholder="your-account@example.com" />
                  <Input label="Chrome Profile 路径" k="chrome_user_data_dir" placeholder="~/Library/Application Support/Google/Chrome" />
                  <Input label="Chrome CDP 地址" k="chrome_cdp_url" placeholder="http://localhost:9222" />
                  <p className="text-xs text-[var(--text-muted)]">
                    第三方账号走后台浏览器自动时，建议先配置 Chrome Profile 或 Chrome CDP，以便复用本机已登录的浏览器会话。
                  </p>
                </>
              )}
            </CardContent>
          </Card>

          {form.identity_provider === 'mailbox' && (
            <Card>
              <CardHeader><CardTitle>系统邮箱配置</CardTitle></CardHeader>
              <CardContent className="space-y-4">
                {optionsError && (
                  <div className="rounded-2xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                    {optionsError}
                  </div>
                )}
                <Select label="邮箱服务" k="mail_provider" options={mailboxProviderOptions.length > 0 ? mailboxProviderOptions : [['moemail', 'MoeMail (sall.cc)']]} />
                {currentMailboxProvider?.description ? (
                  <p className="text-xs leading-5 text-[var(--text-muted)]">{currentMailboxProvider.description}</p>
                ) : null}
                {(currentMailboxProvider?.fields || []).map(renderProviderField)}
              </CardContent>
            </Card>
          )}
        </div>

        <div className="space-y-5 xl:sticky xl:top-4 xl:self-start">
          <Card className="bg-[var(--bg-pane)]/62">
            <CardHeader>
              <CardTitle>当前编排摘要</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                <div className="rounded-[22px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-4">
                  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]"><Mail className="h-3.5 w-3.5" /> Platform</div>
                  <div className="mt-2 text-base font-medium text-[var(--text-primary)]">{currentPlatform?.display_name || form.platform}</div>
                </div>
                <div className="rounded-[22px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-4">
                  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]"><ShieldCheck className="h-3.5 w-3.5" /> Identity</div>
                  <div className="mt-2 text-base font-medium text-[var(--text-primary)]">{summaryRegistration}</div>
                </div>
                <div className="rounded-[22px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-4">
                  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]"><Workflow className="h-3.5 w-3.5" /> Executor</div>
                  <div className="mt-2 text-base font-medium text-[var(--text-primary)]">{summaryExecutor}</div>
                </div>
                <div className="rounded-[22px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-4">
                  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]"><ScanText className="h-3.5 w-3.5" /> Verification</div>
                  <div className="mt-2 text-base font-medium text-[var(--text-primary)]">{summaryVerification}</div>
                </div>
              </div>
              <Button onClick={submit} disabled={polling} className="w-full">
                {polling ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />注册中...</> : <><Play className="mr-2 h-4 w-4" />开始注册</>}
              </Button>
            </CardContent>
          </Card>

          {task ? (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    执行状态
                    <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>
                      {getTaskStatusText(task.status)}
                    </Badge>
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-2">
                    {activeTaskStats.map(({ label, value, icon: Icon }) => (
                      <div key={label} className="rounded-[20px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
                          <Icon className="h-3.5 w-3.5" />
                          {label}
                        </div>
                        <div className="mt-2 text-sm font-medium text-[var(--text-primary)]">{value}</div>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-[20px] border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3 text-xs text-[var(--text-secondary)]">
                    <div>任务 ID</div>
                    <div className="mt-1 break-all font-mono text-[var(--text-primary)]">{task.id}</div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {isActiveTaskStatus(task.status) ? (
                      <Button size="sm" variant="destructive" onClick={cancelTask} disabled={taskActionLoading !== null}>
                        <Square className="mr-1.5 h-3.5 w-3.5" />
                        {taskActionLoading === 'cancel' ? '中断中...' : '中断任务'}
                      </Button>
                    ) : null}
                    {canRetryTask(task) ? (
                      <Button size="sm" variant="outline" onClick={retryTask} disabled={taskActionLoading !== null}>
                        <RotateCcw className={`mr-1.5 h-3.5 w-3.5 ${taskActionLoading === 'retry' ? 'animate-spin' : ''}`} />
                        {taskActionLoading === 'retry' ? '重试中...' : '重试任务'}
                      </Button>
                    ) : null}
                    <Button size="sm" variant="outline" asChild>
                      <a href="/history">去任务记录</a>
                    </Button>
                  </div>
                  <div className="text-xs text-[var(--text-muted)]">
                    关闭当前窗口后，也可以在任务记录页继续查看日志、手动中断或重试。
                  </div>
                  {task.errors?.length > 0 && (
                    <div className="space-y-1">
                      {task.errors.map((e: string, i: number) => (
                        <div key={i} className="flex items-center gap-2 text-red-400">
                          <XCircle className="h-4 w-4" />
                          <span className="text-xs">{e}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {task.error && (
                    <div className="flex items-center gap-2 text-red-400">
                      <XCircle className="h-4 w-4" />
                      <span className="text-xs">{task.error}</span>
                    </div>
                  )}
                  {task.status === 'interrupted' && !task.error && (
                    <div className="flex items-center gap-2 text-amber-400">
                      <XCircle className="h-4 w-4" />
                      <span className="text-xs">任务在服务重启后被中断</span>
                    </div>
                  )}
                  {task.status === 'cancelled' && !task.error && (
                    <div className="flex items-center gap-2 text-amber-400">
                      <XCircle className="h-4 w-4" />
                      <span className="text-xs">任务已取消</span>
                    </div>
                  )}
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle>实时日志</CardTitle></CardHeader>
                <CardContent>
                  <TaskLogPanel taskId={task.id} onDone={handleTaskDone} />
                </CardContent>
              </Card>
            </>
          ) : (
            <Card className="bg-[var(--bg-pane)]/55">
              <CardHeader><CardTitle>等待执行</CardTitle></CardHeader>
              <CardContent className="text-sm text-[var(--text-secondary)]">创建后显示状态与日志。</CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
