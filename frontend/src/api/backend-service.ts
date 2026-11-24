import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export type ExportReceipt = {
  evidenceId: string | null
  version: string | null
  manifestDigest: string | null
  reviewConclusion: string | null
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<ExportReceipt> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? `${key}.csv`
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
  return {
    evidenceId: response.headers.get('X-Evidence-Id'),
    version: response.headers.get('X-Evidence-Version'),
    manifestDigest: response.headers.get('X-Manifest-Digest'),
    reviewConclusion: decodeHeader(response.headers.get('X-Review-Conclusion')),
  }
}

// 响应头只能放 latin-1，后端对中文结论做了百分号编码，这里还原后展示。
function decodeHeader(value: string | null): string | null {
  if (!value) return value
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
