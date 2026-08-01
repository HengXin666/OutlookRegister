export function cn(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ")
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return "未记录"
  const rounded = Math.max(0, Math.round(seconds))
  if (rounded < 60) return `${rounded} 秒`
  const minutes = Math.floor(rounded / 60)
  const remainder = rounded % 60
  if (minutes < 60) return `${minutes} 分 ${remainder.toString().padStart(2, "0")} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${(minutes % 60).toString().padStart(2, "0")} 分`
}

export function formatBytes(bytes: number | null | undefined) {
  if (!bytes) return "未采集"
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "未记录"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return "未记录"
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date)
}

export function formatPercent(value: number, total: number) {
  if (!total) return "0%"
  return `${Math.round((value / total) * 100)}%`
}
