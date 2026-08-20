import { useEffect, useState } from 'react'
import { apiFetch, copyToClipboard } from '@/lib/utils'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Plus, Pencil, Trash2, ToggleLeft, ToggleRight, CreditCard, ShieldCheck, CircleOff, Activity, X, Eye, EyeOff, Copy, Check } from 'lucide-react'

type PaymentCard = {
  id: number
  pan: string
  masked_pan: string
  last4: string
  brand: string
  exp_month: number
  exp_year: number
  holder_name: string
  region: string
  note: string
  is_active: boolean
  success_count: number
  fail_count: number
}

type CardDetail = PaymentCard & {
  pan: string
  cvv: string
}

const emptyForm = {
  pan: '',
  exp_month: '',
  exp_year: '',
  cvv: '',
  holder_name: '',
  region: '',
  note: '',
}

const brandLabel: Record<string, string> = {
  visa: 'Visa',
  mastercard: 'Mastercard',
  amex: 'Amex',
  unknown: '未知',
}

function apiErrorMessage(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : fallback
  try {
    const parsed = JSON.parse(raw) as { detail?: string }
    return parsed.detail || raw
  } catch {
    return raw
  }
}

function formatExpiry(month: number, year: number): string {
  return `${String(month).padStart(2, '0')}/${year}`
}

export default function Cards() {
  const [cards, setCards] = useState<PaymentCard[]>([])
  const [createOpen, setCreateOpen] = useState(false)
  const [viewId, setViewId] = useState<number | null>(null)
  const [editId, setEditId] = useState<number | null>(null)
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null)

  const load = () => apiFetch('/cards').then(setCards)

  useEffect(() => { load() }, [])

  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), 1800)
    return () => clearTimeout(timer)
  }, [toast])

  const copyText = async (label: string, value: string): Promise<boolean> => {
    if (!value) return false
    const ok = await copyToClipboard(value)
    setToast({ text: ok ? `已复制${label}` : `${label}复制失败，请手动选中`, ok })
    return ok
  }

  const del = async (id: number) => {
    await apiFetch(`/cards/${id}`, { method: 'DELETE' })
    load()
  }

  const toggle = async (id: number) => {
    await apiFetch(`/cards/${id}/toggle`, { method: 'PATCH' })
    load()
  }

  const activeCount = cards.filter((item) => item.is_active).length
  const totalSuccess = cards.reduce((sum, item) => sum + Number(item.success_count || 0), 0)
  const totalFail = cards.reduce((sum, item) => sum + Number(item.fail_count || 0), 0)
  const metricCards = [
    { label: '卡数量', value: cards.length, icon: CreditCard, tone: 'text-[var(--accent)]' },
    { label: '启用', value: activeCount, icon: ShieldCheck, tone: 'text-emerald-400' },
    { label: '成功次数', value: totalSuccess, icon: Activity, tone: 'text-[var(--accent)]' },
    { label: '失败次数', value: totalFail, icon: CircleOff, tone: 'text-red-400' },
  ]

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-semibold text-[var(--text-primary)]">支付卡</div>
            <Badge variant="default">总量 {cards.length}</Badge>
            <Badge variant="secondary">活跃 {activeCount}</Badge>
          </div>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4 mr-1.5" />
            添加到卡池
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label} className="bg-transparent">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
              </div>
              <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)]">
                <Icon className={`h-5 w-5 ${tone}`} />
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Card className="overflow-hidden p-0">
        <div className="border-b border-[var(--border)] px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
          卡列表
        </div>
        <div className="glass-table-wrap">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                <th className="px-4 py-2.5 text-left">卡号</th>
                <th className="px-4 py-2.5 text-left">品牌</th>
                <th className="px-4 py-2.5 text-left">有效期</th>
                <th className="px-4 py-2.5 text-left">国家</th>
                <th className="px-4 py-2.5 text-left">成功/失败</th>
                <th className="px-4 py-2.5 text-left">状态</th>
                <th className="px-4 py-2.5 text-left">操作</th>
              </tr>
            </thead>
            <tbody>
              {cards.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-8">
                    <div className="empty-state-panel">当前卡池为空，可以点击右上角添加到卡池。</div>
                  </td>
                </tr>
              )}
              {cards.map((card) => (
                <tr
                  key={card.id}
                  className="border-b border-[var(--border)]/40 hover:bg-[var(--bg-hover)]/70 cursor-pointer"
                  onClick={() => setViewId(card.id)}
                >
                  <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-secondary)]">
                    <div>{card.pan || card.masked_pan}</div>
                    {(card.holder_name || card.note) && (
                      <div className="mt-1 text-[11px] text-[var(--text-muted)]">
                        {[card.holder_name, card.note].filter(Boolean).join(' · ')}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-[var(--text-muted)]">{brandLabel[card.brand] || card.brand || '-'}</td>
                  <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-secondary)]">
                    {formatExpiry(card.exp_month, card.exp_year)}
                  </td>
                  <td className="px-4 py-2.5 text-[var(--text-muted)]">{card.region || '-'}</td>
                  <td className="px-4 py-2.5">
                    <span className="text-emerald-400">{card.success_count}</span>
                    <span className="text-[var(--text-muted)]"> / </span>
                    <span className="text-red-400">{card.fail_count}</span>
                  </td>
                  <td className="px-4 py-2.5">
                    <Badge variant={card.is_active ? 'success' : 'danger'}>
                      {card.is_active ? '活跃' : '禁用'}
                    </Badge>
                  </td>
                  <td className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center gap-2">
                      <button onClick={() => setViewId(card.id)} className="table-action-btn">
                        <Eye className="mr-1.5 h-4 w-4" />
                        查看
                      </button>
                      <button onClick={() => setEditId(card.id)} className="table-action-btn">
                        <Pencil className="mr-1.5 h-4 w-4" />
                        编辑
                      </button>
                      <button onClick={() => toggle(card.id)} className="table-action-btn">
                        {card.is_active ? <ToggleRight className="mr-1.5 h-4 w-4" /> : <ToggleLeft className="mr-1.5 h-4 w-4" />}
                        {card.is_active ? '停用' : '启用'}
                      </button>
                      <button onClick={() => del(card.id)} className="table-action-btn table-action-btn-danger">
                        <Trash2 className="mr-1.5 h-4 w-4" />
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {createOpen && (
        <CardFormDialog
          cardId={null}
          onClose={() => setCreateOpen(false)}
          onDone={() => { setCreateOpen(false); load() }}
        />
      )}
      {editId !== null && (
        <CardFormDialog
          cardId={editId}
          onClose={() => setEditId(null)}
          onDone={() => { setEditId(null); load() }}
        />
      )}
      {viewId !== null && (
        <CardViewDialog
          cardId={viewId}
          onClose={() => setViewId(null)}
          onEdit={() => { setViewId(null); setEditId(viewId) }}
          onCopy={copyText}
        />
      )}
      {toast && (
        <div
          className={`fixed bottom-6 left-1/2 z-[60] -translate-x-1/2 rounded-lg border px-4 py-2 text-sm ${
            toast.ok
              ? 'border-emerald-500/25 bg-emerald-500/12 text-emerald-300'
              : 'border-red-500/25 bg-red-500/12 text-red-300'
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  )
}

function maskDisplay(value: string, kind: 'pan' | 'cvv' | 'expiry' | 'text'): string {
  if (!value) return '-'
  if (kind === 'cvv') return '•••'
  if (kind === 'expiry') return '••/••••'
  if (kind === 'pan') return '•'.repeat(Math.max(12, value.length))
  return '••••••'
}

function CopyRow({
  label,
  value,
  kind,
  revealed,
  onCopy,
}: {
  label: string
  value: string
  kind: 'pan' | 'cvv' | 'expiry' | 'text'
  revealed: boolean
  onCopy: (label: string, value: string) => Promise<boolean>
}) {
  const [copied, setCopied] = useState(false)
  const display = value ? (revealed ? value : maskDisplay(value, kind)) : '-'

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const handleClick = async () => {
    if (!value) return
    if (await onCopy(label, value)) setCopied(true)
  }

  return (
    <button
      type="button"
      className={`flex w-full items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5 text-left transition-colors ${
        copied
          ? 'border-emerald-500/40 bg-emerald-500/10'
          : 'border-[var(--border-soft)] bg-[var(--bg-pane)]/45 hover:bg-[var(--bg-hover)]/70'
      }`}
      onClick={handleClick}
    >
      <div className="min-w-0">
        <div className="text-[11px] text-[var(--text-muted)]">{label}</div>
        <div className={`mt-0.5 truncate text-sm text-[var(--text-primary)] ${kind === 'text' ? '' : 'font-mono'}`}>{display}</div>
      </div>
      {value ? (
        copied ? (
          <span className="flex shrink-0 items-center gap-1 text-xs text-emerald-400">
            <Check className="h-3.5 w-3.5" />
            已复制
          </span>
        ) : (
          <Copy className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
        )
      ) : null}
    </button>
  )
}

function CardViewDialog({
  cardId,
  onClose,
  onEdit,
  onCopy,
}: {
  cardId: number
  onClose: () => void
  onEdit: () => void
  onCopy: (label: string, value: string) => Promise<boolean>
}) {
  const [detail, setDetail] = useState<CardDetail | null>(null)
  const [error, setError] = useState('')
  const [revealed, setRevealed] = useState(false)

  useEffect(() => {
    apiFetch(`/cards/${cardId}`)
      .then(setDetail)
      .catch((err) => setError(apiErrorMessage(err, '加载失败')))
  }, [cardId])

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">支付卡详情</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setRevealed((value) => !value)}
              className="table-action-btn"
            >
              {revealed ? <EyeOff className="mr-1.5 h-4 w-4" /> : <Eye className="mr-1.5 h-4 w-4" />}
              {revealed ? '隐藏' : '显示'}
            </button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4 space-y-2">
          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-2.5 text-xs leading-5 text-red-400">
              {error}
            </div>
          )}
          {!detail && !error && <div className="text-sm text-[var(--text-muted)]">加载中...</div>}
          {detail && (
            <>
              <CopyRow label="卡号" value={detail.pan} kind="pan" revealed={revealed} onCopy={onCopy} />
              <CopyRow label="有效期" value={formatExpiry(detail.exp_month, detail.exp_year)} kind="expiry" revealed={revealed} onCopy={onCopy} />
              <CopyRow label="CVV" value={detail.cvv} kind="cvv" revealed={revealed} onCopy={onCopy} />
              <CopyRow label="持卡人" value={detail.holder_name} kind="text" revealed={revealed} onCopy={onCopy} />
              <div className="pt-1 text-xs text-[var(--text-muted)]">内容默认隐藏，点击即可复制；可用右上角显示按钮查看明文。</div>
            </>
          )}
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={onEdit} className="flex-1">编辑</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">关闭</Button>
        </div>
      </div>
    </div>
  )
}

function CardFormDialog({
  cardId,
  onClose,
  onDone,
}: {
  cardId: number | null
  onClose: () => void
  onDone: () => void
}) {
  const isEdit = cardId !== null
  const [form, setForm] = useState(emptyForm)
  const [loading, setLoading] = useState(isEdit)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const setField = (key: keyof typeof emptyForm, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  useEffect(() => {
    if (cardId === null) return
    apiFetch(`/cards/${cardId}`)
      .then((detail: CardDetail) => {
        setForm({
          pan: detail.pan || '',
          exp_month: String(detail.exp_month),
          exp_year: String(detail.exp_year),
          cvv: detail.cvv || '',
          holder_name: detail.holder_name || '',
          region: detail.region || '',
          note: detail.note || '',
        })
      })
      .catch((err) => setError(apiErrorMessage(err, '加载失败')))
      .finally(() => setLoading(false))
  }, [cardId])

  const save = async () => {
    if (!form.pan.trim() || !form.cvv.trim() || !form.exp_month.trim() || !form.exp_year.trim()) return
    setSaving(true)
    setError('')
    try {
      const payload = {
        pan: form.pan,
        exp_month: Number(form.exp_month),
        exp_year: Number(form.exp_year),
        cvv: form.cvv,
        holder_name: form.holder_name,
        region: form.region,
        note: form.note,
      }
      if (isEdit && cardId !== null) {
        await apiFetch(`/cards/${cardId}`, {
          method: 'PATCH',
          body: JSON.stringify(payload),
        })
      } else {
        await apiFetch('/cards', {
          method: 'POST',
          body: JSON.stringify(payload),
        })
      }
      onDone()
    } catch (err) {
      setError(apiErrorMessage(err, isEdit ? '保存失败' : '添加失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">{isEdit ? '编辑支付卡' : '添加到卡池'}</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-3">
          {loading ? (
            <div className="text-sm text-[var(--text-muted)]">加载中...</div>
          ) : (
            <>
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">卡号</label>
                <input value={form.pan} onChange={(e) => setField('pan', e.target.value)} className="control-surface control-surface-mono" />
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="text-xs text-[var(--text-muted)] block mb-1">月</label>
                  <input value={form.exp_month} onChange={(e) => setField('exp_month', e.target.value)} placeholder="MM" className="control-surface control-surface-mono" />
                </div>
                <div>
                  <label className="text-xs text-[var(--text-muted)] block mb-1">年</label>
                  <input value={form.exp_year} onChange={(e) => setField('exp_year', e.target.value)} placeholder="YY / YYYY" className="control-surface control-surface-mono" />
                </div>
                <div>
                  <label className="text-xs text-[var(--text-muted)] block mb-1">CVV</label>
                  <input value={form.cvv} onChange={(e) => setField('cvv', e.target.value)} className="control-surface control-surface-mono" />
                </div>
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">持卡人（可选）</label>
                <input value={form.holder_name} onChange={(e) => setField('holder_name', e.target.value)} className="control-surface" />
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">国家标签</label>
                <input value={form.region} onChange={(e) => setField('region', e.target.value)} placeholder="如 US, SG" className="control-surface" />
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">备注（可选）</label>
                <input value={form.note} onChange={(e) => setField('note', e.target.value)} className="control-surface" />
              </div>
            </>
          )}
          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-2.5 text-xs leading-5 text-red-400">
              {error}
            </div>
          )}
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={save} disabled={saving || loading} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}
