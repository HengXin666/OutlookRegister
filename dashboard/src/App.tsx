import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock3,
  Database,
  ExternalLink,
  Gauge,
  HardDriveDownload,
  KeyRound,
  ListFilter,
  LoaderCircle,
  Mail,
  PauseCircle,
  Play,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  TimerReset,
  Wifi,
  X,
} from "lucide-react"
import { Badge, Button, Card, Progress } from "./components/ui"
import { ConfigPanel } from "./components/ConfigPanel"
import { WorkflowPanel } from "./components/WorkflowPanel"
import { cn } from "./lib/utils"
import { formatBytes, formatDate, formatDuration, formatPercent } from "./lib/utils"

type StageKey = "registered" | "recovery_bound" | "oauth_authorized" | "hx_email_imported"
type FilterKey = "all" | "complete" | "incomplete" | "failed"

type StageState = { ok: boolean; at: string | null }
type TrafficMetric = { key: string; label: string; bytes: number; human: string; estimated?: boolean }
type AccountActionName = "authorize" | "import_hx_email" | "keepalive"
type AccountActionState = {
  email: string
  action: AccountActionName
  status: "queued" | "running" | "succeeded" | "failed" | "manual_verification_required"
  message: string
  updated_at: string
}
type AccountActionMap = Record<string, Partial<Record<AccountActionName, AccountActionState>>>
type Account = {
  email: string
  identity_countries: string[]
  status: "complete" | "incomplete" | "failed"
  current_stage: string
  current_stage_label: string
  first_seen: string | null
  last_seen: string | null
  duration_seconds: number | null
  duration_human: string | null
  stage_durations: Record<string, number | null>
  stage_timestamps: Record<string, string | null>
  stages: Record<StageKey, StageState>
  latest_detail: string
  recovery: { bound: boolean; email: string; reason: string; detail: string }
  events: Array<{ stage: string; detail: string; timestamp: string | null }>
  recovery_events: Array<{ bound: boolean; recovery_email: string; reason: string; detail: string; timestamp: string | null }>
  traffic: { available: boolean; total_bytes: number; human: string; by_stage: TrafficMetric[] }
}

type Dashboard = {
  generated_at: string
  summary: {
    total: number
    registered: number
    recovery_bound: number
    oauth_authorized: number
    hx_email_imported: number
    fully_complete: number
    average_duration_seconds: number | null
    average_duration_human: string | null
    last_seen: string | null
  }
  stages: Array<{
    key: StageKey
    label: string
    completed: number
    total: number
    average_seconds: number | null
    average_human: string | null
    samples: number
  }>
  duration_averages: Record<string, { average_seconds: number | null; samples: number }>
  traffic: {
    available: boolean
    file: string
    sample_count: number
    total_bytes: number
    human: string
    by_stage: TrafficMetric[]
    by_source: TrafficMetric[]
    note: string
  }
  accounts: Account[]
}

const stageIcons: Record<StageKey, typeof CheckCircle2> = {
  registered: ShieldCheck,
  recovery_bound: CheckCircle2,
  oauth_authorized: ExternalLink,
  hx_email_imported: Database,
}

const stageDurationLabels: Record<string, string> = {
  registration: "注册",
  recovery: "密保绑定",
  oauth: "OAuth 授权",
  hx_email: "加入 HX-Email",
}

const stageOrder: StageKey[] = ["registered", "recovery_bound", "oauth_authorized", "hx_email_imported"]

function statusTone(status: Account["status"]) {
  if (status === "complete") return "success" as const
  if (status === "failed") return "danger" as const
  return "warning" as const
}

function statusLabel(status: Account["status"]) {
  if (status === "complete") return "全部完成"
  if (status === "failed") return "执行失败"
  return "进行中"
}

function recoveryReasonLabel(reason: string) {
  const labels: Record<string, string> = {
    binding_failed: "绑定失败",
    disabled: "功能未启用",
    not_requested: "未请求密保邮箱",
    verified: "Microsoft 已验证",
    verification_failed: "验证失败",
  }
  return labels[reason] || reason || "未记录原因"
}

function StageMark({ state, compact = false }: { state: StageState; compact?: boolean }) {
  return state.ok ? (
    <span className={cn("inline-flex shrink-0 items-center justify-center rounded-full bg-emerald-100 text-emerald-700", compact ? "h-5 w-5" : "h-6 w-6")}>
      <Check className={compact ? "h-3 w-3" : "h-3.5 w-3.5"} strokeWidth={2.5} />
    </span>
  ) : (
    <span className={cn("inline-flex shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-400", compact ? "h-5 w-5" : "h-6 w-6")}>
      <Circle className={compact ? "h-3 w-3" : "h-3.5 w-3.5"} />
    </span>
  )
}

function StageCard({ stage, index }: { stage: Dashboard["stages"][number]; index: number }) {
  const Icon = stageIcons[stage.key]
  const ratio = stage.total ? (stage.completed / stage.total) * 100 : 0
  return (
    <Card className="min-w-0 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-teal-50 text-teal-700">
            <Icon className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-xs font-semibold uppercase tracking-[0.08em] text-slate-400">0{index + 1}</p>
            <h2 className="truncate text-sm font-semibold text-slate-800">{stage.label}</h2>
          </div>
        </div>
        <span className="shrink-0 text-lg font-semibold tabular-nums text-slate-900">{stage.completed}<span className="text-sm font-normal text-slate-400">/{stage.total}</span></span>
      </div>
      <Progress value={ratio} className="mt-4" />
      <div className="mt-3 flex items-center justify-between gap-2 text-xs text-slate-500">
        <span>{formatPercent(stage.completed, stage.total)} 完成</span>
        <span className="truncate">平均 {stage.average_human || "未记录"}</span>
      </div>
    </Card>
  )
}

function TrafficBars({ metrics, emptyLabel }: { metrics: TrafficMetric[]; emptyLabel: string }) {
  const max = Math.max(...metrics.map((metric) => metric.bytes), 1)
  if (metrics.length === 0) return <p className="text-sm text-slate-400">{emptyLabel}</p>
  return (
    <div className="space-y-4">
      {metrics.map((metric) => (
        <div key={metric.key}>
          <div className="mb-1.5 flex items-center justify-between gap-3 text-sm">
            <span className="min-w-0 truncate font-medium text-slate-700">{metric.label}</span>
            <span className="shrink-0 tabular-nums text-slate-500">{metric.human}</span>
          </div>
          <Progress value={(metric.bytes / max) * 100} />
        </div>
      ))}
    </div>
  )
}

function ActionStateRow({ state, onResume }: { state: AccountActionState; onResume?: () => void }) {
  const running = state.status === "queued" || state.status === "running"
  const failed = state.status === "failed"
  const waitingForOperator = state.status === "manual_verification_required"
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md border px-3 py-2.5 text-xs",
        failed ? "border-red-200 bg-red-50 text-red-700" : state.status === "succeeded" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : waitingForOperator ? "border-amber-200 bg-amber-50 text-amber-800" : "border-sky-200 bg-sky-50 text-sky-700",
      )}
      role="status"
    >
      {running ? <LoaderCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" /> : failed ? <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : waitingForOperator ? <PauseCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      <span className="min-w-0 flex-1 break-words leading-5">{state.message}</span>
      {waitingForOperator && onResume && <Button variant="ghost" className="h-7 shrink-0 px-2 text-[11px]" onClick={onResume} title="人工验证完成后继续"><Play className="h-3 w-3" />继续</Button>}
      <span className="shrink-0 text-[11px] opacity-70">{formatDate(state.updated_at)}</span>
    </div>
  )
}

function AccountDetail({
  account,
  actions,
  onAction,
  onResume,
  onClose,
}: {
  account: Account
  actions: Partial<Record<AccountActionName, AccountActionState>>
  onAction: (account: Account, action: AccountActionName) => void
  onResume: (account: Account, action: AccountActionName) => void
  onClose: () => void
}) {
  const activeAction = Object.values(actions).find((state) => state && (state.status === "queued" || state.status === "running" || state.status === "manual_verification_required"))
  const canAuthorize = account.stages.registered.ok && !activeAction
  const canImport = account.stages.oauth_authorized.ok && !activeAction
  const canKeepalive = account.stages.registered.ok && !activeAction
  const recoveryTone = account.recovery.bound ? "success" : account.recovery.email ? "warning" : "neutral"
  const recoveryStatus = account.recovery.bound ? "已确认绑定" : account.recovery.email ? "已选择，未确认" : "未记录"
  return (
    <>
      <button className="fixed inset-0 z-30 cursor-default bg-slate-900/20 backdrop-blur-[1px]" onClick={onClose} aria-label="关闭详情" />
      <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-5">
          <div className="min-w-0">
            <div className="mb-2 flex items-center gap-2">
              <Badge tone={statusTone(account.status)}>{statusLabel(account.status)}</Badge>
              <span className="text-xs text-slate-400">{account.current_stage_label}</span>
            </div>
            <h2 className="break-all text-lg font-semibold text-slate-900">{account.email}</h2>
            {account.identity_countries.length > 0 && <div className="mt-2 flex flex-wrap items-center gap-1.5"><span className="text-xs text-slate-400">flow 国家</span>{account.identity_countries.map((country) => <Badge key={country} tone="teal">{country}</Badge>)}</div>}
          </div>
          <Button variant="ghost" className="h-8 w-8 shrink-0 px-0" onClick={onClose} title="关闭详情" aria-label="关闭详情">
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="scrollbar-thin flex-1 overflow-y-auto px-5 py-5">
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-md bg-slate-50 p-3">
              <p className="text-xs text-slate-500">任务用时</p>
              <p className="mt-1 text-base font-semibold text-slate-900">{account.duration_human || "未记录"}</p>
            </div>
            <div className="rounded-md bg-slate-50 p-3">
              <p className="text-xs text-slate-500">观测流量</p>
              <p className="mt-1 text-base font-semibold text-slate-900">{account.traffic.available ? account.traffic.human : "未采集"}</p>
              </div>
          </div>

            <section className="mt-7">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-slate-900">密保邮箱</h3>
                <Badge tone={recoveryTone}>{recoveryStatus}</Badge>
              </div>
              <div
                className={cn(
                  "rounded-md border p-3",
                  account.recovery.bound
                    ? "border-emerald-200 bg-emerald-50/60"
                    : account.recovery.email
                      ? "border-amber-200 bg-amber-50/60"
                      : "border-slate-200 bg-slate-50",
                )}
              >
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-white/80 text-slate-600">
                    <Mail className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-slate-500">
                      {account.recovery.bound ? "当前绑定地址" : account.recovery.email ? "最近选择地址" : "绑定地址"}
                    </p>
                    <p className="mt-1 break-all text-sm font-semibold text-slate-900">
                      {account.recovery.email || "未记录"}
                    </p>
                    <p className="mt-1 text-xs text-slate-600">
                      {account.recovery.bound
                        ? "该地址已通过 Microsoft 验证"
                        : recoveryReasonLabel(account.recovery.reason)}
                    </p>
                  </div>
                </div>
                {account.recovery.detail && <p className="mt-3 border-t border-black/5 pt-3 text-xs leading-5 text-slate-600">{account.recovery.detail}</p>}
              </div>
              {account.recovery_events.length > 0 && (
                <div className="mt-3 space-y-2">
                  <p className="text-xs font-medium text-slate-500">验证记录</p>
                  {account.recovery_events.slice(-5).reverse().map((event, index) => (
                    <div key={`${event.timestamp}-${event.recovery_email}-${index}`} className="rounded-md border border-slate-200 px-3 py-2.5">
                      <div className="flex items-start justify-between gap-3">
                        <span className="min-w-0 break-all text-sm font-medium text-slate-700">{event.recovery_email || "未记录邮箱"}</span>
                        <Badge tone={event.bound ? "success" : "warning"}>{event.bound ? "已绑定" : "未绑定"}</Badge>
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-3 text-xs text-slate-400">
                        <span>{recoveryReasonLabel(event.reason)}</span>
                        <span className="shrink-0">{formatDate(event.timestamp)}</span>
                      </div>
                      {event.detail && <p className="mt-1 break-words text-xs leading-5 text-slate-500">{event.detail}</p>}
                    </div>
                  ))}
                </div>
              )}
            </section>

          <section className="mt-7">
            <h3 className="mb-3 text-sm font-semibold text-slate-900">补充操作</h3>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <Button
                variant={account.stages.oauth_authorized.ok ? "secondary" : "primary"}
                className="w-full"
                disabled={!canAuthorize}
                onClick={() => onAction(account, "authorize")}
                title={account.stages.registered.ok ? "执行 OAuth 授权" : "账号注册完成后才可授权"}
              >
                {activeAction?.action === "authorize" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
                {account.stages.oauth_authorized.ok ? "重新授权" : "补充授权"}
              </Button>
              <Button
                className="w-full"
                disabled={!canImport}
                onClick={() => onAction(account, "import_hx_email")}
                title={account.stages.oauth_authorized.ok ? "导入 HX-Email" : "OAuth 授权完成后才可导入"}
              >
                {activeAction?.action === "import_hx_email" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
                {account.stages.hx_email_imported.ok ? "重新加入 HX-Email" : "加入 HX-Email"}
              </Button>
              <Button
                variant="secondary"
                className="w-full"
                disabled={!canKeepalive}
                onClick={() => onAction(account, "keepalive")}
                title="使用账号面板执行保活登录"
              >
                {activeAction?.action === "keepalive" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                保活登录
              </Button>
            </div>
            {Object.values(actions).length > 0 && (
              <div className="mt-3 space-y-2">
                {Object.values(actions).filter((state): state is AccountActionState => Boolean(state)).map((state) => <ActionStateRow key={state.action} state={state} onResume={() => onResume(account, state.action)} />)}
              </div>
            )}
          </section>

          <section className="mt-7">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">阶段记录</h3>
              <span className="text-xs text-slate-400">{formatDate(account.first_seen)} - {formatDate(account.last_seen)}</span>
            </div>
            <div className="space-y-2">
              {stageOrder.map((key) => {
                const state = account.stages[key]
                const at = account.stage_timestamps[key]
                return (
                  <div key={key} className="flex items-center gap-3 rounded-md border border-slate-200 px-3 py-2.5">
                    <StageMark state={state} />
                    <span className={cn("min-w-0 flex-1 text-sm", state.ok ? "font-medium text-slate-800" : "text-slate-400")}>
                      {key === "registered" ? "已注册" : key === "recovery_bound" ? "已绑定密保邮箱" : key === "oauth_authorized" ? "已完成 OAuth 授权" : "已加入 HX-Email"}
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-slate-400">{formatDate(at)}</span>
                  </div>
                )
              })}
            </div>
          </section>

          <section className="mt-7">
            <h3 className="mb-3 text-sm font-semibold text-slate-900">阶段耗时</h3>
            <div className="divide-y divide-slate-100 rounded-md border border-slate-200">
              {Object.entries(stageDurationLabels).map(([key, label]) => (
                <div key={key} className="flex items-center justify-between gap-3 px-3 py-2.5 text-sm">
                  <span className="text-slate-600">{label}</span>
                  <span className="font-medium tabular-nums text-slate-900">{formatDuration(account.stage_durations[key])}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="mt-7">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-slate-900">流量分阶段</h3>
              {account.traffic.available && <Badge tone="teal">观测值</Badge>}
            </div>
            <TrafficBars metrics={account.traffic.by_stage} emptyLabel="该任务尚未采集流量" />
          </section>

          <section className="mt-7">
            <h3 className="mb-3 text-sm font-semibold text-slate-900">最近检查点</h3>
            <div className="space-y-2">
              {account.events.slice(-8).reverse().map((event, index) => (
                <div key={`${event.stage}-${event.timestamp}-${index}`} className="border-l-2 border-slate-200 pl-3">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium text-slate-700">{event.stage}</span>
                    <span className="shrink-0 text-xs text-slate-400">{formatDate(event.timestamp)}</span>
                  </div>
                  {event.detail && <p className="mt-1 break-words text-xs leading-5 text-slate-500">{event.detail}</p>}
                </div>
              ))}
            </div>
          </section>
        </div>
      </aside>
    </>
  )
}

function App() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<FilterKey>("all")
  const [selected, setSelected] = useState<Account | null>(null)
  const [accountActions, setAccountActions] = useState<AccountActionMap>({})
  const [activeView, setActiveView] = useState<"dashboard" | "workflows" | "config">("dashboard")

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const [dashboardResponse, actionsResponse] = await Promise.all([
        fetch("/api/dashboard", { cache: "no-store" }),
        fetch("/api/account-actions", { cache: "no-store" }),
      ])
      if (!dashboardResponse.ok) throw new Error(`HTTP ${dashboardResponse.status}`)
      if (!actionsResponse.ok) throw new Error(`操作状态 HTTP ${actionsResponse.status}`)
      const actionsPayload = (await actionsResponse.json()) as { accounts?: AccountActionMap }
      setData((await dashboardResponse.json()) as Dashboard)
      setAccountActions(actionsPayload.accounts || {})
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法读取面板数据")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, refresh])

  const runAccountAction = useCallback(async (account: Account, action: AccountActionName) => {
    const key = account.email.toLowerCase()
    const optimistic: AccountActionState = {
      email: account.email,
      action,
      status: "queued",
      message: "正在提交操作",
      updated_at: new Date().toISOString(),
    }
    setAccountActions((current) => ({
      ...current,
      [key]: { ...current[key], [action]: optimistic },
    }))
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(account.email)}/actions/${action.replace(/_/g, "-")}`, {
        method: "POST",
      })
      const payload = (await response.json()) as { action?: AccountActionState; detail?: string }
      if (!response.ok || !payload.action) throw new Error(payload.detail || `HTTP ${response.status}`)
      setAccountActions((current) => ({
        ...current,
        [key]: { ...current[key], [action]: payload.action },
      }))
    } catch (err) {
      setAccountActions((current) => ({
        ...current,
        [key]: {
          ...current[key],
          [action]: {
            ...optimistic,
            status: "failed",
            message: err instanceof Error ? err.message : "操作提交失败",
            updated_at: new Date().toISOString(),
          },
        },
      }))
    }
  }, [])

  const resumeAccountAction = useCallback(async (account: Account, action: AccountActionName) => {
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(account.email)}/actions/${action.replace(/_/g, "-")}/resume`, { method: "POST" })
      const payload = (await response.json()) as { action?: AccountActionState; detail?: string }
      if (!response.ok || !payload.action) throw new Error(payload.detail || `HTTP ${response.status}`)
      setAccountActions((current) => ({ ...current, [account.email.toLowerCase()]: { ...current[account.email.toLowerCase()], [action]: payload.action! } }))
    } catch (err) {
      setError(err instanceof Error ? err.message : "继续操作失败")
    }
  }, [])

  useEffect(() => {
    if (selected && data) {
      setSelected(data.accounts.find((account) => account.email === selected.email) || null)
    }
  }, [data, selected])

  const accounts = useMemo(() => {
    if (!data) return []
    const normalized = query.trim().toLowerCase()
    return data.accounts.filter((account) => {
      const matchesQuery = !normalized || account.email.toLowerCase().includes(normalized)
      const matchesFilter = filter === "all" || account.status === filter
      return matchesQuery && matchesFilter
    })
  }, [data, filter, query])

  if (loading && !data) {
    return <div className="flex min-h-screen items-center justify-center bg-[#f4f7f6] text-sm text-slate-500"><LoaderCircle className="mr-2 h-4 w-4 animate-spin" />正在读取任务记录</div>
  }

  if (error && !data) {
    return <div className="flex min-h-screen items-center justify-center bg-[#f4f7f6] p-6"><Card className="max-w-md p-6"><div className="flex items-center gap-3 text-red-700"><AlertCircle className="h-5 w-5" /><h1 className="font-semibold">面板数据读取失败</h1></div><p className="mt-3 text-sm text-slate-500">{error}</p><Button className="mt-5" onClick={() => void refresh()}><RefreshCw className="h-4 w-4" />重试</Button></Card></div>
  }

  if (!data) return null
  if (activeView === "config") {
    return (
      <div className="min-h-screen bg-[#f4f7f6]">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-[1480px] flex-wrap items-center justify-between gap-4 px-4 py-5 sm:px-6 lg:px-8">
            <div className="flex items-center gap-3"><div className="flex h-10 w-10 items-center justify-center rounded-md bg-slate-900 text-white"><Settings2 className="h-5 w-5" /></div><div><p className="text-xs font-semibold uppercase tracking-[0.12em] text-teal-700">Outlook Register / Settings</p><h1 className="mt-1 text-xl font-semibold text-slate-950">运行配置</h1></div></div>
            <Button variant="ghost" onClick={() => setActiveView("dashboard")} title="返回任务面板"><Activity className="h-4 w-4" />任务面板</Button>
          </div>
        </header>
        <main className="mx-auto max-w-[1480px] px-4 py-6 sm:px-6 lg:px-8"><ConfigPanel /></main>
      </div>
    )
  }
  const summary = data.summary
  const totalTraffic = data.traffic.total_bytes

  return (
    <div className="min-h-screen bg-[#f4f7f6]">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-[1480px] flex-col gap-4 px-4 py-5 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-slate-900 text-white"><Activity className="h-5 w-5" /></div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-teal-700">Outlook Register / Operations</p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight text-slate-950">注册任务状态面板</h1>
              <p className="mt-1 text-sm text-slate-500">账号阶段、执行时间与网络观测</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 p-1">
              <Button variant={activeView === "dashboard" ? "primary" : "ghost"} className="h-8 px-2.5" onClick={() => setActiveView("dashboard")} title="查看账号任务">任务</Button>
              <Button variant={activeView === "workflows" ? "primary" : "ghost"} className="h-8 px-2.5" onClick={() => setActiveView("workflows")} title="查看注册与保活工作流">工作流</Button>
            </div>
            <Button variant="ghost" className="h-8 px-2.5" onClick={() => setActiveView("config")} title="打开运行配置"><Settings2 className="h-4 w-4" />配置</Button>
            <div className="flex h-9 items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 text-xs text-slate-500"><span className={cn("h-2 w-2 rounded-full", autoRefresh ? "bg-emerald-500" : "bg-slate-300")} />{autoRefresh ? "每 5 秒刷新" : "手动刷新"}</div>
            <Button variant="ghost" onClick={() => setAutoRefresh((value) => !value)} title="切换自动刷新" aria-label="切换自动刷新"><TimerReset className="h-4 w-4" />{autoRefresh ? "暂停" : "自动刷新"}</Button>
            <Button variant="primary" onClick={() => void refresh()} disabled={refreshing} title="立即刷新" aria-label="立即刷新"><RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />刷新</Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1480px] space-y-6 px-4 py-6 sm:px-6 lg:px-8">
        {activeView === "workflows" && <WorkflowPanel accounts={data.accounts} />}
        {activeView === "dashboard" && <>
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Card className="p-4 sm:col-span-2 xl:col-span-1">
            <div className="flex items-center gap-2 text-xs font-medium text-slate-500"><Gauge className="h-4 w-4 text-teal-600" />任务总量</div>
            <p className="mt-3 text-3xl font-semibold tabular-nums text-slate-950">{summary.total}</p>
            <p className="mt-1 text-xs text-slate-500">全部完成 <span className="font-semibold text-emerald-700">{summary.fully_complete}</span></p>
          </Card>
          <Card className="p-4">
            <div className="flex items-center gap-2 text-xs font-medium text-slate-500"><Clock3 className="h-4 w-4 text-sky-600" />平均任务用时</div>
            <p className="mt-3 text-2xl font-semibold tabular-nums text-slate-950">{summary.average_duration_human || "未记录"}</p>
            <p className="mt-1 text-xs text-slate-500">已计算 {data.duration_averages.total.samples} 个任务</p>
          </Card>
          <Card className="p-4">
            <div className="flex items-center gap-2 text-xs font-medium text-slate-500"><HardDriveDownload className="h-4 w-4 text-amber-600" />观测流量</div>
            <p className="mt-3 text-2xl font-semibold tabular-nums text-slate-950">{data.traffic.available ? formatBytes(totalTraffic) : "未采集"}</p>
            <p className="mt-1 truncate text-xs text-slate-500">{data.traffic.available ? `${data.traffic.sample_count} 条网络观测` : "历史记录没有流量字段"}</p>
          </Card>
          <Card className="p-4 sm:col-span-2 xl:col-span-2">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-xs font-medium text-slate-500"><Wifi className="h-4 w-4 text-violet-600" />数据状态</div>
              <Badge tone={data.traffic.available ? "success" : "warning"}>{data.traffic.available ? "流量已接入" : "等待流量采集"}</Badge>
            </div>
            <div className="mt-4 flex flex-wrap items-end justify-between gap-4">
              <div><p className="text-sm font-semibold text-slate-900">最后事件</p><p className="mt-1 text-xs text-slate-500">{formatDate(summary.last_seen)}</p></div>
              <div className="text-left sm:text-right"><p className="text-sm font-semibold text-slate-900">已更新</p><p className="mt-1 text-xs text-slate-500">{formatDate(data.generated_at)}</p></div>
            </div>
          </Card>
        </section>

        <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {data.stages.map((stage, index) => <StageCard key={stage.key} stage={stage} index={index} />)}
        </section>

        <section className="grid gap-6 xl:grid-cols-[1.35fr_0.65fr]">
          <Card className="p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Traffic ledger</p><h2 className="mt-1 text-base font-semibold text-slate-900">流量分阶段</h2></div>
              <Badge tone={data.traffic.available ? "teal" : "neutral"}>{data.traffic.available ? "观测值" : "未采集"}</Badge>
            </div>
            <div className="mt-5"><TrafficBars metrics={data.traffic.by_stage} emptyLabel="历史 account_checkpoints.jsonl 未记录流量" /></div>
            <p className="mt-5 border-t border-slate-100 pt-4 text-xs leading-5 text-slate-400">{data.traffic.note}</p>
          </Card>
          <Card className="p-5">
            <div className="flex items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Timing</p><h2 className="mt-1 text-base font-semibold text-slate-900">阶段平均用时</h2></div><SlidersHorizontal className="h-4 w-4 text-slate-400" /></div>
            <div className="mt-4 divide-y divide-slate-100">
              {data.stages.map((stage) => <div key={stage.key} className="flex items-center justify-between gap-3 py-3"><span className="text-sm text-slate-600">{stage.label}</span><span className="text-sm font-semibold tabular-nums text-slate-900">{stage.average_human || "未记录"}</span></div>)}
            </div>
          </Card>
        </section>

        <section>
          <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Account ledger</p><h2 className="mt-1 text-base font-semibold text-slate-900">账号任务</h2></div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <label className="relative block sm:w-72"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 Outlook 地址" className="h-9 w-full rounded-md border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-800 outline-none placeholder:text-slate-400 focus:border-teal-500 focus:ring-2 focus:ring-teal-500/10" /></label>
              <label className="relative block sm:w-36"><ListFilter className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><select value={filter} onChange={(event) => setFilter(event.target.value as FilterKey)} className="h-9 w-full appearance-none rounded-md border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-700 outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-500/10"><option value="all">全部状态</option><option value="complete">全部完成</option><option value="incomplete">进行中</option><option value="failed">执行失败</option></select></label>
            </div>
          </div>
          <Card className="overflow-hidden">
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[980px] border-collapse text-left">
                <thead className="border-b border-slate-200 bg-slate-50/80 text-xs font-semibold text-slate-500"><tr><th className="px-4 py-3">账号</th>{stageOrder.map((key) => <th key={key} className="px-3 py-3 text-center">{key === "registered" ? "注册" : key === "recovery_bound" ? "密保" : key === "oauth_authorized" ? "授权" : "HX-Email"}</th>)}<th className="px-3 py-3">任务用时</th><th className="px-3 py-3">流量</th><th className="w-8 px-2 py-3" /></tr></thead>
                <tbody className="divide-y divide-slate-100">
                  {accounts.map((account) => <tr key={account.email} onClick={() => setSelected(account)} className="cursor-pointer bg-white transition-colors hover:bg-teal-50/40"><td className="max-w-[280px] px-4 py-3.5"><div className="truncate text-sm font-medium text-slate-800">{account.email}</div><div className="mt-1 flex items-center gap-2"><Badge tone={statusTone(account.status)}>{statusLabel(account.status)}</Badge><span className="truncate text-xs text-slate-400">{formatDate(account.last_seen)}</span></div></td>{stageOrder.map((key) => <td key={key} className="px-3 py-3.5 text-center"><div className="flex justify-center" title={account.stages[key].ok ? "已完成" : "未完成"}><StageMark state={account.stages[key]} compact /></div></td>)}<td className="px-3 py-3.5 text-sm tabular-nums text-slate-600">{account.duration_human || "未记录"}</td><td className="px-3 py-3.5 text-sm tabular-nums text-slate-600">{account.traffic.available ? account.traffic.human : "未采集"}</td><td className="px-2 py-3.5"><ChevronRight className="h-4 w-4 text-slate-300" /></td></tr>)}
                </tbody>
              </table>
            </div>
            <div className="divide-y divide-slate-100 md:hidden">
              {accounts.map((account) => <button key={account.email} onClick={() => setSelected(account)} className="block w-full px-4 py-4 text-left transition-colors hover:bg-teal-50/40"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-medium text-slate-800">{account.email}</p><div className="mt-1 flex items-center gap-2"><Badge tone={statusTone(account.status)}>{statusLabel(account.status)}</Badge><span className="truncate text-xs text-slate-400">{account.duration_human || "未记录"}</span></div></div><ChevronRight className="mt-1 h-4 w-4 shrink-0 text-slate-300" /></div><div className="mt-4 grid grid-cols-4 gap-2">{stageOrder.map((key) => <div key={key} className="flex items-center gap-1.5 text-xs text-slate-500"><StageMark state={account.stages[key]} compact /><span>{key === "registered" ? "注册" : key === "recovery_bound" ? "密保" : key === "oauth_authorized" ? "授权" : "HX"}</span></div>)}</div></button>)}
            </div>
            {accounts.length === 0 && <div className="px-5 py-12 text-center text-sm text-slate-400">没有符合条件的账号</div>}
          </Card>
          <div className="mt-3 flex items-center justify-between text-xs text-slate-400"><span>显示 {accounts.length} / {data.accounts.length} 个账号</span><span>点击行查看阶段时间与流量明细</span></div>
        </section>
        </>}
      </main>
      {selected && (
        <AccountDetail
          account={selected}
          actions={accountActions[selected.email.toLowerCase()] || {}}
          onAction={(account, action) => void runAccountAction(account, action)}
          onResume={(account, action) => void resumeAccountAction(account, action)}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

export default App
