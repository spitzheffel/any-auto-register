import { useEffect, useState } from 'react'
import { Camera, Download } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { attemptArtifactPath, getArtifactFileLabel } from '@/lib/tasks'
import { apiDownload, triggerBrowserDownload } from '@/lib/utils'

const SCREENSHOT = 'screenshot.png'

/** 步骤失败时落盘的现场包：截图预览 + 其余文件下载。 */
export function TaskFailureArtifacts({
  taskId,
  attemptId,
  bundle,
  files,
  category,
  error,
}: {
  taskId: string
  attemptId: string
  bundle: string
  files: string[]
  category?: string
  error?: string
}) {
  const [preview, setPreview] = useState('')
  const [downloading, setDownloading] = useState('')
  const fileKey = files.join(',')

  useEffect(() => {
    if (!files.includes(SCREENSHOT)) return
    let objectUrl = ''
    let cancelled = false
    apiDownload(attemptArtifactPath(taskId, attemptId, bundle, SCREENSHOT))
      .then(({ blob }) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setPreview(objectUrl)
      })
      .catch(() => {
        // 现场包可能已被保留策略清掉，不打断日志面板
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, attemptId, bundle, fileKey])

  const download = async (filename: string) => {
    setDownloading(filename)
    try {
      const { blob } = await apiDownload(attemptArtifactPath(taskId, attemptId, bundle, filename))
      triggerBrowserDownload(blob, `${attemptId}-${bundle}-${filename}`)
    } catch {
      // 同上，静默失败
    } finally {
      setDownloading('')
    }
  }

  // 网络记录里有未脱敏的邮箱 / 手机号 / token，只给下载不在界面上摊开
  const downloads = files.filter(name => name !== SCREENSHOT)

  return (
    <div className="my-1.5 rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2.5 font-sans">
      <div className="flex flex-wrap items-center gap-2">
        <Camera className="h-3.5 w-3.5 flex-shrink-0 text-red-400" />
        <span className="text-xs font-medium text-red-400">失败现场</span>
        <span className="font-mono text-[11px] text-[var(--text-muted)]">{bundle}</span>
        {category ? (
          <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-[11px] text-red-300">
            {category}
          </span>
        ) : null}
      </div>

      {error ? (
        <p className="mt-1 break-words text-[11px] leading-4 text-[var(--text-secondary)]">{error}</p>
      ) : null}

      {preview ? (
        <img
          src={preview}
          alt="失败截图"
          title="点击查看大图"
          className="mt-2 max-h-40 cursor-zoom-in rounded border border-[var(--border)] object-contain"
          onClick={() => window.open(preview, '_blank')}
        />
      ) : null}

      {downloads.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {downloads.map(name => (
            <Button
              key={name}
              type="button"
              variant="outline"
              size="sm"
              disabled={downloading === name}
              onClick={() => download(name)}
            >
              <Download className="mr-1 h-3 w-3" />
              {downloading === name ? '下载中...' : getArtifactFileLabel(name)}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
