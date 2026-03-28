import { useEffect, useState, useRef, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel, listProviderFieldKeys } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { buildExecutorOptions, buildRegistrationOptions, hasReusableOAuthBrowser, pickOAuthExecutor } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Copy, ExternalLink, Download, Upload, Plus, X, Mail, WalletCards, ShieldCheck, Inbox, ScanSearch, Trash2 } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()

function getAccountOverview(acc: any) {
  return acc?.overview || {}
}

function getVerificationMailbox(acc: any) {
  const providerResources = Array.isArray(acc?.provider_resources) ? acc.provider_resources : []
  const normalized = providerResources.find((item: any) => item?.resource_type === 'mailbox')
  if (normalized) {
    return {
      provider: normalized.provider_name,
      email: normalized.handle || normalized.display_name,
      account_id: normalized.resource_identifier,
    }
  }
  return null
}

function getLifecycleStatus(acc: any) {
  return acc?.lifecycle_status || 'registered'
}

function getDisplayStatus(acc: any) {
  return acc?.display_status || acc?.plan_state || getLifecycleStatus(acc)
}

function getPlanState(acc: any) {
  return acc?.plan_state || acc?.overview?.plan_state || 'unknown'
}

function getValidityStatus(acc: any) {
  return acc?.validity_status || acc?.overview?.validity_status || 'unknown'
}

function getCompactStatusMeta(acc: any) {
  return `生命周期:${getLifecycleStatus(acc)} / 套餐:${getPlanState(acc)} / 有效:${getValidityStatus(acc)}`
}

function getProviderAccounts(acc: any) {
  return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : []
}

function getCredentials(acc: any) {
  return Array.isArray(acc?.credentials) ? acc.credentials : []
}

function getCashierUrl(acc: any) {
  const overview = getAccountOverview(acc)
  return overview?.cashier_url || acc?.cashier_url || ''
}

function getPrimaryToken(acc: any) {
  if (acc?.primary_token) return acc.primary_token
  const credential = getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.credential_type === 'token' && item?.value)
  return credential?.value || ''
}

function getPlatformCredentialValue(acc: any, keys: string[]) {
  const credentials = getCredentials(acc)
  for (const key of keys) {
    const credential = credentials.find((item: any) => item?.scope === 'platform' && item?.key === key && item?.value)
    if (credential?.value) return String(credential.value)
  }
  return ''
}

function getGrokSso(acc: any) {
  return getPlatformCredentialValue(acc, ['sso'])
}

function getGrokSsoRw(acc: any) {
  return getPlatformCredentialValue(acc, ['sso_rw', 'sso-rw'])
}

function escapeCsvField(value: unknown) {
  const text = value == null ? '' : String(value)
  if (!/[",\n\r]/.test(text)) return text
  return `"${text.replace(/"/g, '""')}"`
}

async function loadPlatformActions(platform: string) {
  const key = String(platform || '').trim()
  if (!key) return []
  if (platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
}

// ── 注册弹框 ────────────────────────────────────────────────
function RegisterModal({
  platform,
  platformMeta,
  onClose,
  onDone,
}: {
  platform: string
  platformMeta: any
  onClose: () => void
  onDone: () => void
}) {
  const [config, setConfig] = useState<any | null>(null)
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
  })
  const [configLoading, setConfigLoading] = useState(true)
  const [regCount, setRegCount] = useState(1)
  const [concurrency, setConcurrency] = useState(1)
  const [selection, setSelection] = useState({
    identityProvider: '',
    oauthProvider: '',
    executorType: '',
  })
  const [taskId, setTaskId] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [starting, setStarting] = useState(false)

  const supportedExecutors: string[] = platformMeta?.supported_executors || ['protocol']
  const registrationOptions = buildRegistrationOptions(platformMeta)
  const reusableBrowser = hasReusableOAuthBrowser(config || {})
  const executorOptions = buildExecutorOptions(selection.identityProvider, supportedExecutors, reusableBrowser)
  const selectedRegistration = registrationOptions.find(option =>
    option.identityProvider === selection.identityProvider && option.oauthProvider === selection.oauthProvider,
  )
  const selectedExecutor = executorOptions.find(option => option.value === selection.executorType)

  useEffect(() => {
    let active = true
    setConfigLoading(true)
    Promise.all([
      getConfig().catch(() => ({})),
      getConfigOptions().catch(() => null),
    ])
      .then(([cfg, options]) => {
        if (!active) return
        setConfig(cfg || {})
        if (options) {
          setConfigOptions(options)
        }
      })
      .catch(() => {
        if (!active) return
        setConfig({})
        setConfigOptions({ mailbox_providers: [], captcha_providers: [], mailbox_settings: [], captcha_settings: [], captcha_policy: {} })
      })
      .finally(() => {
        if (active) setConfigLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (configLoading || registrationOptions.length === 0) return
    const cfg = config || {}
    const defaultRegistration = registrationOptions.find(option =>
      option.identityProvider === cfg.default_identity_provider &&
      (option.identityProvider !== 'oauth_browser' || option.oauthProvider === (cfg.default_oauth_provider || '')),
    ) || registrationOptions[0]
    setSelection((current) => {
      const identityProvider = current.identityProvider || defaultRegistration.identityProvider
      const oauthProvider = identityProvider === 'oauth_browser'
        ? (current.oauthProvider || defaultRegistration.oauthProvider)
        : ''
      const validExecutorOptions = buildExecutorOptions(identityProvider, supportedExecutors, hasReusableOAuthBrowser(cfg))
        .filter(option => !option.disabled)
      const preferredExecutor = identityProvider === 'oauth_browser'
        ? pickOAuthExecutor(supportedExecutors, cfg.default_executor || 'headed', hasReusableOAuthBrowser(cfg))
        : ((cfg.default_executor && supportedExecutors.includes(cfg.default_executor)) ? cfg.default_executor : supportedExecutors[0] || 'protocol')
      const executorType = validExecutorOptions.some(option => option.value === current.executorType)
        ? current.executorType
        : (validExecutorOptions.find(option => option.value === preferredExecutor)?.value || validExecutorOptions[0]?.value || '')
      if (
        current.identityProvider === identityProvider &&
        current.oauthProvider === oauthProvider &&
        current.executorType === executorType
      ) {
        return current
      }
      return { identityProvider, oauthProvider, executorType }
    })
  }, [config, configLoading, registrationOptions, supportedExecutors])

  useEffect(() => {
    if (!selection.identityProvider) return
    const validExecutorOptions = buildExecutorOptions(selection.identityProvider, supportedExecutors, reusableBrowser)
      .filter(option => !option.disabled)
    if (!validExecutorOptions.some(option => option.value === selection.executorType)) {
      setSelection(current => {
        const nextExecutorType = validExecutorOptions[0]?.value || ''
        if (current.executorType === nextExecutorType) {
          return current
        }
        return {
          ...current,
          executorType: nextExecutorType,
        }
      })
    }
  }, [selection.identityProvider, selection.oauthProvider, selection.executorType, supportedExecutors, reusableBrowser])

  const start = async () => {
    setStarting(true)
    try {
      const cfg = config || {}
      const extra: Record<string, any> = {
        identity_provider: selection.identityProvider,
        oauth_provider: selection.oauthProvider,
        oauth_email_hint: cfg.oauth_email_hint,
        chrome_user_data_dir: cfg.chrome_user_data_dir,
        chrome_cdp_url: cfg.chrome_cdp_url,
        mail_provider: cfg.mail_provider || 'moemail',
      }
      listProviderFieldKeys([
        ...(configOptions.mailbox_providers || []),
        ...(configOptions.captcha_providers || []),
      ]).forEach((fieldKey) => {
        if (cfg[fieldKey] !== undefined) {
          extra[fieldKey] = cfg[fieldKey]
        }
      })
      if (extra.solver_url === undefined || extra.solver_url === '') {
        extra.solver_url = 'http://localhost:8889'
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform, count: regCount, concurrency,
          executor_type: selection.executorType,
          captcha_solver: 'auto',
          proxy: null,
          extra,
        }),
      })
      setTaskId(res.task_id)
    } finally { setStarting(false) }
  }

  const handleDone = () => {
    setDone(true)
    onDone()
  }

  return (
    <div className="dialog-backdrop" onClick={!taskId ? onClose : undefined}>
      <div className="dialog-panel dialog-panel-md flex flex-col"
           onClick={e => e.stopPropagation()} style={{maxHeight: '88vh'}}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">注册 {platformMeta?.display_name || platform}</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 flex-1 overflow-y-auto flex flex-col gap-5">
          {!taskId ? (
            configLoading ? (
              <div className="text-sm text-[var(--text-muted)]">正在加载注册配置...</div>
            ) : (
              <>
                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 1</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">选择注册身份</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">当前平台支持什么，这里就显示什么，不再让你先研究平台能力配置。</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {registrationOptions.map(option => {
                      const active = selection.identityProvider === option.identityProvider && selection.oauthProvider === option.oauthProvider
                      return (
                        <button
                          key={option.key}
                          type="button"
                          onClick={() => setSelection(current => ({
                            ...current,
                            identityProvider: option.identityProvider,
                            oauthProvider: option.oauthProvider,
                          }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            active
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                            {option.identityProvider === 'mailbox' ? <Mail className="h-4 w-4" /> : null}
                            {option.label}
                          </div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                        </button>
                      )
                    })}
                  </div>
                </div>

                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 2</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">选择执行方式</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">所有方式都自动执行，只是协议或浏览器通道不同。</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-3">
                    {executorOptions.map(option => {
                      const active = selection.executorType === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          disabled={option.disabled}
                          onClick={() => !option.disabled && setSelection(current => ({ ...current, executorType: option.value }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            option.disabled
                              ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                              : active
                                ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                                : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="text-sm font-medium text-[var(--text-primary)]">{option.label}</div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                          {option.reason ? (
                            <div className="mt-2 text-xs text-amber-400">{option.reason}</div>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">注册数量</label>
                    <input type="number" min={1} max={99} value={regCount}
                      onChange={e => setRegCount(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">并发数</label>
                    <input type="number" min={1} max={5} value={concurrency}
                      onChange={e => setConcurrency(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                </div>

                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  <div>注册身份: <span className="text-[var(--text-primary)]">{selectedRegistration?.label || '-'}</span></div>
                  <div className="mt-1">执行方式: <span className="text-[var(--text-primary)]">{selectedExecutor?.label || '-'}</span></div>
                  <div className="mt-1">验证策略: <span className="text-[var(--text-primary)]">{getCaptchaStrategyLabel(selection.executorType)}</span></div>
                  {selection.identityProvider === 'oauth_browser' && !reusableBrowser && (
                    <div className="mt-2 text-amber-400">后台浏览器自动依赖 Chrome Profile 或 Chrome CDP，未配置时只允许可视浏览器自动。</div>
                  )}
                </div>

                <Button
                  onClick={start}
                  disabled={starting || !selection.identityProvider || !selection.executorType}
                  className="w-full"
                >
                  {starting ? '启动中...' : '开始自动注册'}
                </Button>
              </>
            )
          ) : (
            <TaskLogPanel taskId={taskId} onDone={handleDone} />
          )}
        </div>
        <div className="px-6 py-3 border-t border-[var(--border)] flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>
            {done ? '关闭' : '取消'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── 新增账号弹框 ─────────────────────────────────────────
function AddModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ email: '', password: '', lifecycle_status: 'registered', primary_token: '', cashier_url: '' })
  const [saving, setSaving] = useState(false)
  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch('/accounts', {
        method: 'POST',
        body: JSON.stringify({ ...form, platform }),
      })
      onDone()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">手动新增账号</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          {[['email','邮箱','text'],['password','密码','text'],['primary_token','主凭证','text'],['cashier_url','试用链接','text']].map(([k,l,t]) => (
            <div key={k}>
              <label className="text-xs text-[var(--text-muted)] block mb-1">{l}</label>
              <input type={t} value={(form as any)[k]} onChange={e => set(k, e.target.value)}
                className="control-surface" />
            </div>
          ))}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => set('lifecycle_status', e.target.value)}
              className="control-surface appearance-none">
              <option value="registered">已注册</option>
              <option value="trial">试用中</option>
              <option value="subscribed">已订阅</option>
            </select>
          </div>
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null

  const stats: Array<{ label: string; value: any }> = []
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.usage_summary?.plan_title) stats.push({ label: 'Kiro 套餐', value: payload.usage_summary.plan_title })
  if ('days_until_reset' in (payload.usage_summary || {})) stats.push({ label: '重置倒计时', value: payload.usage_summary?.days_until_reset })
  if (payload.usage_summary?.next_reset_at) stats.push({ label: '下次重置', value: payload.usage_summary.next_reset_at })
  if ('available' in (payload.portal_session || {})) stats.push({ label: 'Portal 可用', value: payload.portal_session?.available })
  if (payload.desktop_app_state?.app_name) stats.push({ label: '桌面应用', value: payload.desktop_app_state?.app_name })
  if ('running' in (payload.desktop_app_state || {})) stats.push({ label: '桌面已打开', value: payload.desktop_app_state?.running })
  if ('ready' in (payload.desktop_app_state || {})) stats.push({ label: '桌面就绪', value: payload.desktop_app_state?.ready })

  const cursorModels = payload.usage_summary?.models && typeof payload.usage_summary.models === 'object'
    ? Object.entries(payload.usage_summary.models)
    : []
  const kiroBreakdowns = Array.isArray(payload.usage_summary?.breakdowns)
    ? payload.usage_summary.breakdowns
    : []
  const kiroPlans = Array.isArray(payload.usage_summary?.plans)
    ? payload.usage_summary.plans
    : []

  if (stats.length === 0 && cursorModels.length === 0 && kiroBreakdowns.length === 0 && kiroPlans.length === 0 && !payload.quota_note) {
    return null
  }

  return (
    <div className="space-y-4 mb-4">
      {stats.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
        </div>
      )}

      {cursorModels.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Cursor Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {cursorModels.map(([model, info]: [string, any]) => (
              <div key={model} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{model}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>请求数: {formatResultValue(info?.num_requests)}</div>
                  <div>总请求: {formatResultValue(info?.num_requests_total)}</div>
                  <div>Token: {formatResultValue(info?.num_tokens)}</div>
                  <div>剩余请求: {formatResultValue(info?.remaining_requests)}</div>
                  <div>请求上限: {formatResultValue(info?.max_request_usage)}</div>
                  <div>Token 上限: {formatResultValue(info?.max_token_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroBreakdowns.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroBreakdowns.map((item: any, index: number) => (
              <div key={`${item.resource_type || item.display_name}-${index}`} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item.display_name || item.resource_type}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>已用: {formatResultValue(item.current_usage)}</div>
                  <div>上限: {formatResultValue(item.usage_limit)}</div>
                  <div>剩余: {formatResultValue(item.remaining_usage)}</div>
                  <div>单位: {formatResultValue(item.unit)}</div>
                  <div>试用状态: {formatResultValue(item.trial_status)}</div>
                  <div>试用到期: {formatResultValue(item.trial_expiry)}</div>
                  <div>试用上限: {formatResultValue(item.trial_usage_limit)}</div>
                  <div>试用剩余: {formatResultValue(item.trial_remaining_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroPlans.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Plans</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroPlans.map((plan: any) => (
              <div key={plan.name} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">{plan.title || plan.name}</div>
                  <div className="text-xs text-emerald-400">{formatResultValue(plan.amount)} {plan.currency || ''}</div>
                </div>
                <div className="mt-1 text-[11px] text-[var(--text-muted)]">{plan.billing_interval || '-'}</div>
                {Array.isArray(plan.features) && plan.features.length > 0 && (
                  <div className="mt-2 text-xs text-[var(--text-secondary)] break-words">
                    {plan.features.join(' · ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {payload.quota_note && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
          {payload.quota_note}
        </div>
      )}
    </div>
  )
}

function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)] rounded-xl p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

function ActionTaskModal({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md flex flex-col"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '88vh' }}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">平台操作任务日志</p>
          </div>
          {taskStatus ? (
            <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
              {getTaskStatusText(taskStatus)}
            </Badge>
          ) : null}
        </div>
        <div className="px-6 py-4 flex-1 overflow-y-auto">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="px-6 py-3 border-t border-[var(--border)] flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>
            关闭
          </Button>
        </div>
      </div>
    </div>
  )
}
// ── 行操作菜单 ─────────────────────────────────────────────
function ActionMenu({
  acc,
  onDetail,
  onDelete,
  onResult,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
}) {
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [running, setRunning] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let active = true
    loadPlatformActions(acc.platform)
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    return () => {
      active = false
    }
  }, [acc.platform])
  useEffect(() => {
    if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t) }
  }, [toast])
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const handleActionDone = async (status: string) => {
    if (!actionTask) return
    setActionTaskStatus(status)
    setRunning(null)
    try {
      const task = await apiFetch(`/tasks/${actionTask.taskId}`)
      const data = task?.data ?? task?.result?.data
      if (status !== 'succeeded') {
        setToast({ type: 'error', text: task?.error || getTaskStatusText(status) })
        return
      }
      const actionUrl = data?.url || data?.checkout_url || data?.cashier_url
      if (actionUrl) {
        window.open(actionUrl, '_blank')
      }
      if (data && typeof data === 'object') {
        const detailKeys = Object.keys(data).filter(key => !['message', 'url', 'checkout_url', 'cashier_url'].includes(key))
        if (detailKeys.length > 0) {
          onResult(actionTask.title, data)
        }
        setToast({ type: 'success', text: data.message || '操作成功' })
        return
      }
      setToast({ type: 'success', text: typeof data === 'string' && data ? data : '操作成功' })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || '读取任务结果失败' })
    }
  }

  return (
    <div className="relative flex min-w-[136px] items-center justify-end gap-1.5 whitespace-nowrap">
      {toast && (
        <div className={`fixed top-4 right-4 z-50 px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium animate-in fade-in slide-in-from-top-2 ${
          toast.type === 'success' ? 'bg-emerald-500/90 text-white' : 'bg-red-500/90 text-white'
        }`} onClick={() => setToast(null)}>
          {toast.type === 'success' ? '✓ ' : '✗ '}{toast.text}
        </div>
      )}
      {actionTask && (
        <ActionTaskModal
          title={actionTask.title}
          taskId={actionTask.taskId}
          taskStatus={actionTaskStatus}
          onClose={() => {
            setActionTask(null)
            setActionTaskStatus(null)
          }}
          onDone={handleActionDone}
        />
      )}
      <button onClick={onDetail} className="table-action-btn">详情</button>
      {actions.length > 0 && (
        <div className="relative" ref={menuRef}>
          <button onClick={() => setOpen(o => !o)}
            className="table-action-btn">更多 ▾</button>
          {open && (
            <div className="absolute right-0 top-11 z-20 min-w-[140px] overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)] backdrop-blur-xl">
              {actions.map(a => (
                <button key={a.id}
                  onClick={() => {
                    setOpen(false)
                    setRunning(a.id)
                    setActionTaskStatus(null)
                    apiFetch(`/actions/${acc.platform}/${acc.id}/${a.id}`, { method: 'POST', body: JSON.stringify({ params: {} }) })
                      .then(task => {
                        if (!task?.task_id) {
                          setRunning(null)
                          setToast({ type: 'error', text: '任务创建失败' })
                          return
                        }
                        setActionTask({
                          taskId: task.task_id,
                          title: `${acc.email} · ${a.label}`,
                        })
                      })
                      .catch(() => {
                        setRunning(null)
                        setToast({ type: 'error', text: '请求失败' })
                      })
                  }}
                  disabled={!!running}
                  className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50">
                  {running === a.id ? '执行中...' : a.label}
                </button>
              ))}
              <div className="my-1 border-t border-[var(--border)]/70" />
              <button
                onClick={() => {
                  setOpen(false)
                  if (confirm(`确认删除 ${acc.email}？`)) {
                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
                  }
                }}
                className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]"
              >
                删除
              </button>
            </div>
          )}
        </div>
      )}
      {actions.length === 0 && (
        <button
          onClick={() => { if (confirm(`确认删除 ${acc.email}？`)) apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete) }}
          className="table-action-btn table-action-btn-danger"
        >
          删除
        </button>
      )}
    </div>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const [form, setForm] = useState({
    lifecycle_status: getLifecycleStatus(acc),
    primary_token: getPrimaryToken(acc),
    cashier_url: getCashierUrl(acc),
  })
  const [saving, setSaving] = useState(false)
  const overview = getAccountOverview(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const providerAccounts = getProviderAccounts(acc)
  const credentials = getCredentials(acc)
  const copyText = (text: string) => navigator.clipboard.writeText(text)
  const platformCredentials = credentials.filter((item: any) => item.scope === 'platform')

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(form) })
      onSave()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm overflow-y-auto" style={{maxHeight:'90vh'}} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">账号详情</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">{acc.email}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <ResultStat label="展示状态" value={getDisplayStatus(acc)} />
            <ResultStat label="生命周期" value={getLifecycleStatus(acc)} />
            <ResultStat label="有效性" value={getValidityStatus(acc)} />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <ResultStat label="套餐状态" value={getPlanState(acc)} />
            <ResultStat label="套餐名称" value={acc.plan_name || overview.plan_name || overview.plan} />
          </div>
          {(overview?.chips?.length > 0 || verificationMailbox?.email) && (
            <div className="space-y-2">
              {overview?.chips?.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {overview.chips.map((chip: string) => (
                    <span key={chip} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
                      {chip}
                    </span>
                  ))}
                </div>
              )}
              {verificationMailbox?.email && (
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-xs text-[var(--text-secondary)]">
                  验证码邮箱: {verificationMailbox.email} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                </div>
              )}
            </div>
          )}
          {providerAccounts.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Provider Accounts</label>
              {providerAccounts.map((item: any, index: number) => (
                <div key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">
                    {item.provider_name || item.provider_type || 'provider'}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-secondary)] break-all">
                    登录标识: {item.login_identifier || '-'}
                  </div>
                  {item.credentials && Object.keys(item.credentials).length > 0 && (
                    <div className="mt-2 grid gap-2">
                      {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                        <div key={key}>
                          <div className="text-[11px] text-[var(--text-muted)]">{key}</div>
                          <div className="flex items-start gap-1">
                            <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all">
                              {String(value || '-')}
                            </div>
                            {value ? (
                              <button onClick={() => copyText(String(value))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                                <Copy className="h-3 w-3" />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {platformCredentials.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Platform Credentials</label>
              {platformCredentials.map((item: any) => (
                <div key={`${item.scope}-${item.key}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-[11px] text-[var(--text-muted)]">{item.key}</div>
                  <div className="mt-1 flex items-start gap-1">
                    <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all">
                      {item.value}
                    </div>
                    <button onClick={() => copyText(String(item.value || ''))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                      <Copy className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => setForm(f => ({ ...f, lifecycle_status: e.target.value }))}
              className="control-surface appearance-none">
              {['registered','trial','subscribed','expired','invalid'].map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">主凭证</label>
            <textarea value={form.primary_token} onChange={e => setForm(f => ({ ...f, primary_token: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">试用链接</label>
            <textarea value={form.cashier_url} onChange={e => setForm(f => ({ ...f, cashier_url: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

// ── 导入弹框 ────────────────────────────────────────────────
function ImportModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform, lines }) })
      setResult(`导入成功 ${res.created} 个`); onDone()
    } catch (e: any) { setResult(`失败: ${e.message}`) } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function ExportMenu({
  platform,
  total,
  statusFilter,
  searchFilter,
  selectedIds,
}: {
  platform: string
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const doExport = async (format: 'json' | 'csv' | 'cpa' | 'sub2api' | 'sub2api-account') => {
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform,
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
    } catch (e: any) {
      window.alert(e?.message || '导出失败')
    } finally {
      setLoading(null)
    }
  }

  const options = [
    { key: 'json', label: '导出 JSON' },
    { key: 'csv', label: '导出 CSV' },
    { key: 'cpa', label: '导出 CPA' },
    { key: 'sub2api', label: '导出 Sub2Api(旧)' },
    { key: 'sub2api-account', label: '导出 Sub2API Account' },
  ] as const

  return (
    <div className="relative" ref={menuRef}>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(v => !v)}
        disabled={total === 0 || !!loading}
      >
        <Download className="h-4 w-4 mr-1" />
        {loading ? '导出中...' : hasSelection ? `导出已选(${selectedIds.length})` : '导出'}
      </Button>
      {open && (
          <div className="absolute right-0 top-10 z-50 min-w-[148px] rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-lg">
          <div className="px-3 py-1 text-[11px] text-[var(--text-muted)]">
            {hasSelection ? `导出 ${selectedIds.length} 个已选账号` : '导出当前筛选结果'}
          </div>
          {options.map(option => (
            <button
              key={option.key}
              onClick={() => doExport(option.key)}
              className="w-full px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function WorkspaceMetric({
  label,
  value,
  icon: Icon,
}: {
  label: string
  value: string | number
  icon: any
}) {
  return (
    <div className="rounded-[16px] border border-[var(--border)] bg-[var(--bg-pane)]/58 px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
          <div className="mt-0.5 text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
        </div>
        <div className="flex h-8 w-8 items-center justify-center rounded-[12px] border border-[var(--border-soft)] bg-[var(--chip-bg)] text-[var(--accent)]">
          <Icon className="h-3.5 w-3.5" />
        </div>
      </div>
    </div>
  )
}


// ── Main ────────────────────────────────────────────────────
export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const [tab, setTab] = useState(platform || 'trae')
  useEffect(() => { if (platform) { setTab(platform) } }, [platform])

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [deletingSelected, setDeletingSelected] = useState(false)
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach(p => { map[p.name] = p })
      setPlatformsMap(map)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setSelectedIds(new Set())
  }, [tab, filterStatus, debouncedSearch])

  const load = useCallback(async (p = tab, s = debouncedSearch, fs = filterStatus) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: p, page: '1', page_size: '100' })
      if (s) params.set('email', s)
      if (fs) params.set('status', fs)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts(data.items); setTotal(data.total)
    } finally { setLoading(false) }
  }, [tab, debouncedSearch, filterStatus])

  useEffect(() => { load(tab, debouncedSearch, filterStatus) }, [tab, debouncedSearch, filterStatus])

  useEffect(() => {
    setSelectedIds(prev => {
      const visible = new Set(accounts.map(acc => acc.id))
      return new Set([...prev].filter(id => visible.has(id)))
    })
  }, [accounts])

  const exportCsv = () => {
    const baseHeaders = ['email', 'password', 'display_status', 'lifecycle_status', 'plan_state', 'validity_status', 'cashier_url', 'created_at']
    const headers = tab === 'grok' ? [...baseHeaders, 'sso', 'sso_rw'] : baseHeaders
    const rowsSource = selectedIds.size > 0 ? accounts.filter(a => selectedIds.has(a.id)) : accounts
    const rows = rowsSource.map(a => [
      a.email,
      a.password,
      getDisplayStatus(a),
      getLifecycleStatus(a),
      getPlanState(a),
      getValidityStatus(a),
      getCashierUrl(a),
      a.created_at,
      ...(tab === 'grok' ? [getGrokSso(a), getGrokSsoRw(a)] : []),
    ].map(escapeCsvField).join(','))
    const blob = new Blob([[headers.join(','), ...rows].join('\n')], { type: 'text/csv' })
    triggerBrowserDownload(blob, `${tab}_accounts.csv`)
  }

  const pageIds = accounts.map(acc => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))
  const selectedCount = selectedIds.size

  const toggleOne = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach(id => next.delete(id))
      else pageIds.forEach(id => next.add(id))
      return next
    })
  }

  const deleteSelected = async () => {
    if (selectedCount === 0) return
    if (!window.confirm(`确认删除选中的 ${selectedCount} 个账号吗？此操作不可撤销。`)) return
    setDeletingSelected(true)
    try {
      await apiFetch('/accounts/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: [...selectedIds] }),
      })
      setSelectedIds(new Set())
      await load()
    } catch (error: any) {
      window.alert(error?.message || '批量删除失败')
    } finally {
      setDeletingSelected(false)
    }
  }

  const copy = (text: string) => {
    if (navigator.clipboard) { navigator.clipboard.writeText(text) }
    else { const el = document.createElement('textarea'); el.value = text; document.body.appendChild(el); el.select(); document.execCommand('copy'); document.body.removeChild(el) }
  }

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || tab
  const visibleTrial = accounts.filter(acc => getPlanState(acc) === 'trial').length
  const visibleSubscribed = accounts.filter(acc => getPlanState(acc) === 'subscribed').length
  const visibleInvalid = accounts.filter(acc => getValidityStatus(acc) === 'invalid' || getLifecycleStatus(acc) === 'invalid').length
  const linkedCashier = accounts.filter(acc => Boolean(getCashierUrl(acc))).length
  const verificationBacked = accounts.filter(acc => Boolean(getVerificationMailbox(acc)?.email)).length

  return (
    <div className="flex flex-col gap-4">
      {detail && <DetailModal acc={detail} onClose={() => setDetail(null)} onSave={() => { setDetail(null); load() }} />}
      {showImport && <ImportModal platform={tab} onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); load() }} />}
      {showAdd && <AddModal platform={tab} onClose={() => setShowAdd(false)} onDone={() => { setShowAdd(false); load() }} />}
      {showRegister && <RegisterModal platform={tab} platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={() => load()} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <WorkspaceMetric label="账号数" value={total} icon={WalletCards} />
        <WorkspaceMetric label="验证码邮箱" value={verificationBacked} icon={Inbox} />
        <WorkspaceMetric label="已订阅" value={visibleSubscribed} icon={ShieldCheck} />
        <WorkspaceMetric label="可操作" value={linkedCashier} icon={ScanSearch} />
      </div>

      <Card className="z-30 overflow-visible bg-[var(--bg-pane)]/60">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-sm font-semibold text-[var(--text-primary)]">{platformLabel}</div>
              <Badge variant="secondary">{total} 条</Badge>
              {selectedCount > 0 ? <Badge variant="default">已选 {selectedCount}</Badge> : null}
              <Badge variant="success">试用 {visibleTrial}</Badge>
              <Badge variant="default">订阅 {visibleSubscribed}</Badge>
              <Badge variant="warning">链接 {linkedCashier}</Badge>
              <Badge variant={visibleInvalid > 0 ? 'danger' : 'secondary'}>失效 {visibleInvalid}</Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => setShowRegister(true)}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                自动注册
              </Button>
              <Button size="sm" variant="outline" onClick={() => setShowImport(true)}>
                <Upload className="mr-1.5 h-3.5 w-3.5" />
                导入
              </Button>
              {tab === 'chatgpt' ? (
                <ExportMenu
                  platform={tab}
                  total={total}
                  statusFilter={filterStatus}
                  searchFilter={debouncedSearch}
                  selectedIds={[...selectedIds]}
                />
              ) : (
                <Button size="sm" variant="outline" onClick={exportCsv} disabled={accounts.length === 0}>
                  <Download className="mr-1.5 h-3.5 w-3.5" />
                  导出
                </Button>
              )}
              <Button
                size="sm"
                variant="destructive"
                onClick={deleteSelected}
                disabled={selectedCount === 0 || deletingSelected}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {deletingSelected ? '删除中...' : `批量删除${selectedCount > 0 ? `(${selectedCount})` : ''}`}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setShowAdd(true)}>手动新增</Button>
              <Button variant="outline" size="sm" onClick={() => load()} disabled={loading}>
                <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              </Button>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-[minmax(220px,320px)_220px_auto]">
            <div>
              <label className="mb-1.5 block text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">邮箱搜索</label>
              <input
                type="text"
                placeholder="按邮箱搜索当前平台账号"
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="control-surface control-surface-compact"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">状态筛选</label>
              <select
                value={filterStatus}
                onChange={e => setFilterStatus(e.target.value)}
                className="control-surface control-surface-compact appearance-none"
              >
                <option value="">全部状态</option>
                <option value="registered">已注册</option>
                <option value="trial">试用中</option>
                <option value="subscribed">已订阅</option>
                <option value="free">免费</option>
                <option value="eligible">可试用</option>
                <option value="expired">已过期</option>
                <option value="invalid">已失效</option>
              </select>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <Badge variant="secondary">{total} 账号</Badge>
              {debouncedSearch ? <Badge variant="default">搜索中</Badge> : null}
              {filterStatus ? <Badge variant="warning">{filterStatus}</Badge> : null}
              {selectedCount > 0 ? <Badge variant="success">已选 {selectedCount}</Badge> : null}
            </div>
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div>
            <div className="text-sm font-medium text-[var(--text-primary)]">{platformLabel} 账号清单</div>
          </div>
        </div>
        <div className="glass-table-wrap overflow-x-hidden">
        <table className="table-fixed w-full text-sm">
          <colgroup>
            <col className="w-10" />
            <col className="w-[31%]" />
            <col className="w-[14%]" />
            <col className="w-[21%]" />
            <col className="w-[8%]" />
            <col className="w-[10%]" />
            <col className="w-[16%]" />
          </colgroup>
          <thead>
            <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
              <th className="px-4 py-2.5 text-left w-10">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent"
                />
              </th>
              <th className="px-4 py-2.5 text-left">邮箱</th>
              <th className="px-4 py-2.5 text-left">密码</th>
              <th className="px-4 py-2.5 text-left">状态</th>
              <th className="px-4 py-2.5 text-left">试用链接</th>
              <th className="px-4 py-2.5 text-left">注册时间</th>
              <th className="px-4 py-2.5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-8">
                  <div className="empty-state-panel">
                    当前筛选下没有账号记录。你可以直接自动注册、手动新增或导入已有账号。
                  </div>
                </td>
              </tr>
            )}
            {accounts.map(acc => (
              (() => {
                const overview = getAccountOverview(acc)
                const verificationMailbox = getVerificationMailbox(acc)
                return (
              <tr key={acc.id} className="border-b border-[var(--border)]/50 hover:bg-[var(--bg-hover)]/70 transition-colors cursor-pointer"
                  onClick={() => setDetail(acc)}>
                <td className="px-4 py-2.5" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(acc.id)}
                    onChange={() => toggleOne(acc.id)}
                    className="checkbox-accent"
                  />
                </td>
                <td className="px-4 py-2.5 font-mono text-xs align-top">
                  <div className="flex min-w-0 items-center gap-1">
                    <span className="truncate" title={acc.email}>{acc.email}</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.email) }} className="text-[var(--text-muted)] hover:text-[var(--text-secondary)]"><Copy className="h-3 w-3" /></button>
                  </div>
                  {verificationMailbox && (verificationMailbox.email || verificationMailbox.account_id || verificationMailbox.provider) && (
                    <div
                      className="mt-1 truncate text-[11px] text-[var(--text-muted)]"
                      title={`验证码邮箱: ${verificationMailbox.email || '-'} · ${verificationMailbox.provider || '-'} · ID ${verificationMailbox.account_id || '-'}`}
                    >
                      验证码邮箱: {verificationMailbox.email || '-'} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                    </div>
                  )}
                  {overview?.remote_email && overview.remote_email !== acc.email && (
                    <div className="mt-1 truncate text-[11px] text-[var(--text-muted)]" title={`远端邮箱: ${overview.remote_email}`}>
                      远端邮箱: {overview.remote_email}
                    </div>
                  )}
                  {Array.isArray(overview?.chips) && overview.chips.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {overview.chips.slice(0, 4).map((chip: string) => (
                        <span key={chip} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2 py-0.5 text-[10px] text-[var(--text-secondary)]">
                          {chip}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-muted)] align-top">
                  <div className="flex min-w-0 items-center gap-1">
                    <span className="truncate blur-sm transition-all cursor-default hover:blur-none" title={acc.password}>{acc.password}</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.password) }} className="text-[var(--text-muted)] hover:text-[var(--text-secondary)]"><Copy className="h-3 w-3" /></button>
                  </div>
                </td>
                <td className="px-4 py-2.5 align-top">
                  <div className="min-w-0 space-y-1">
                    <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                    <div
                      className="truncate text-[11px] leading-5 text-[var(--text-muted)]"
                      title={getCompactStatusMeta(acc)}
                    >
                      {getCompactStatusMeta(acc)}
                    </div>
                  </div>
                </td>
                <td className="px-4 py-2.5 align-top">
                  {getCashierUrl(acc) ? (
                    <div className="flex items-center gap-1 whitespace-nowrap">
                      <button onClick={e => { e.stopPropagation(); copy(getCashierUrl(acc)) }} className="text-[var(--text-muted)] hover:text-[var(--text-secondary)]"><Copy className="h-3 w-3" /></button>
                      <a href={getCashierUrl(acc)} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="text-[var(--text-muted)] hover:text-[var(--text-accent)]"><ExternalLink className="h-3 w-3" /></a>
                    </div>
                  ) : <span className="text-[var(--text-muted)] text-xs">-</span>}
                </td>
                <td className="px-4 py-2.5 text-[var(--text-muted)] text-xs whitespace-nowrap align-top">
                  {acc.created_at ? new Date(acc.created_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
                </td>
                <td className="px-4 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <ActionMenu
                    acc={acc}
                    onDetail={() => setDetail(acc)}
                    onDelete={() => load()}
                    onResult={(title, payload) => setActionResult({ title, payload })}
                  />
                </td>
              </tr>
                )
              })()
            ))}
          </tbody>
        </table>
        </div>
      </Card>
    </div>
  )
}
