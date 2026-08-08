import { useCallback, useEffect, useMemo, useState } from "react"
import { CheckCircle2, KeyRound, LoaderCircle, LogIn, Pause, Play, RefreshCw, ScrollText, ShieldCheck } from "lucide-react"
import { Badge, Button, Card } from "./ui"
import { cn } from "../lib/utils"

type WorkflowAccount = {
  email: string
  stages: { registered: { ok: boolean } }
}

type WorkflowJob = {
  job_id: string
  kind: string
  status: "queued" | "running" | "succeeded" | "failed"
  total?: number
  concurrency?: number
  message: string
  updated_at: string
}

type AccountActionState = {
  email: string
  action: string
  status: "queued" | "running" | "pausing" | "paused" | "succeeded" | "failed" | "manual_verification_required"
  step?: string
  message: string
  updated_at: string
  logs?: Array<{ timestamp: string; level: "info" | "warning" | "error"; message: string }>
  page_record?: { url: string; title: string; body_text: string; html: string; frames: string[]; captured_at: string }
}

type AccountActionMap = Record<string, Partial<Record<string, AccountActionState>>>

function jobTone(status: WorkflowJob["status"]) {
  if (status === "succeeded") return "success" as const
  if (status === "failed") return "danger" as const
  return status === "running" ? "teal" as const : "warning" as const
}

function actionTone(status: AccountActionState["status"]) {
  if (status === "succeeded") return "success" as const
  if (status === "failed") return "danger" as const
  if (status === "manual_verification_required" || status === "pausing" || status === "paused") return "warning" as const
  return status === "running" ? "teal" as const : "neutral" as const
}

function actionLabel(status: AccountActionState["status"]) {
  if (status === "succeeded") return "完成"
  if (status === "failed") return "失败"
  if (status === "manual_verification_required") return "等待人工验证"
  if (status === "pausing") return "暂停中"
  if (status === "paused") return "已暂停"
  return status === "running" ? "执行中" : "排队"
}

const stepLabels: Record<string, string> = {
  queued: "等待执行",
  starting: "启动任务",
  preparing: "读取配置",
  proxy: "申请住宅代理",
  browser: "启动浏览器",
  login: "打开 Outlook",
  email_login: "邮箱登录",
  email_code: "获取邮箱验证码并提交完成登录",
  manual_challenge: "自动解锁安全验证（警告重按 → 长按挑战 → 恢复页面）",
  oauth: "获取授权",
  hx_email: "加入 HX-Email",
  login_email: "填写邮箱",
  login_password: "填写密码",
  login_options: "处理登录选项",
  unlock: "警告页重新按压",
  unlock_loading: "按压验证加载中",
  unlock_verification: "自动完成按压验证",
  verification: "自动按压验证",
  press_again: "重新按压安全验证",
  unlock_recovery: "恢复登录页面",
  recovery_email_form: "填写密保邮箱",
  recovery_code: "密保邮箱验证码",
  login_retry: "重试登录",
  login_complete: "登录完成",
  oauth_check: "检查 OAuth 授权",
  oauth_authorize: "补充 OAuth 授权",
  finishing: "整理结果",
  complete: "保活完成",
  failed: "执行失败",
}

function stepLabel(step?: string) {
  return step ? stepLabels[step] || step : "等待步骤更新"
}

function logTime(timestamp: string) {
  const parsed = new Date(timestamp)
  return Number.isNaN(parsed.getTime()) ? "--:--:--" : parsed.toLocaleTimeString("zh-CN", { hour12: false })
}

function StepLine({ label, active = false }: { label: string; active?: boolean }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className={cn("flex h-6 w-6 shrink-0 items-center justify-center rounded-full", active ? "bg-teal-700 text-white" : "bg-emerald-50 text-emerald-700")}>
        <CheckCircle2 className="h-3.5 w-3.5" />
      </span>
      <span className={active ? "font-medium text-slate-900" : "text-slate-600"}>{label}</span>
    </div>
  )
}

export function WorkflowPanel({ accounts }: { accounts: WorkflowAccount[] }) {
  const [jobs, setJobs] = useState<Record<string, WorkflowJob>>({})
  const [registrationCount, setRegistrationCount] = useState(1)
  const [registrationConcurrency, setRegistrationConcurrency] = useState(1)
  const [authMode, setAuthMode] = useState<"password" | "recovery">("password")
  const [forceReauth, setForceReauth] = useState(false)
  const [keepaliveEmail, setKeepaliveEmail] = useState<string>("")
  const [accountActions, setAccountActions] = useState<AccountActionMap>({})
  const [submitting, setSubmitting] = useState(false)
  const [controlling, setControlling] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    const [workflowResponse, actionsResponse] = await Promise.all([
      fetch("/api/workflows", { cache: "no-store" }),
      fetch("/api/account-actions", { cache: "no-store" }),
    ])
    if (!workflowResponse.ok) throw new Error(`工作流 HTTP ${workflowResponse.status}`)
    if (!actionsResponse.ok) throw new Error(`保活状态 HTTP ${actionsResponse.status}`)
    const payload = (await workflowResponse.json()) as { jobs?: Record<string, WorkflowJob> }
    const actionPayload = (await actionsResponse.json()) as { accounts?: AccountActionMap }
    setJobs(payload.jobs || {})
    setAccountActions(actionPayload.accounts || {})
  }, [])

  useEffect(() => {
    void refresh().catch((error) => setMessage(error instanceof Error ? error.message : "无法读取工作流状态"))
    const timer = window.setInterval(() => void refresh().catch(() => undefined), 2000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const registeredAccounts = useMemo(() => accounts.filter((account) => account.stages.registered.ok), [accounts])
  const registeredCount = registeredAccounts.length

  const submitRegistration = async () => {
    setSubmitting(true)
    setMessage(null)
    try {
      const response = await fetch("/api/workflows/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: registrationCount, concurrency: registrationConcurrency }),
      })
      const payload = (await response.json()) as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "注册任务提交失败")
    } finally {
      setSubmitting(false)
    }
  }

  const submitKeepalive = async () => {
    setSubmitting(true)
    setMessage(null)
    try {
      const response = await fetch("/api/workflows/keepalive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auth_mode: authMode, force_oauth_reauth: forceReauth, emails: keepaliveEmail ? [keepaliveEmail] : [] }),
      })
      const payload = (await response.json()) as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保活任务提交失败")
    } finally {
      setSubmitting(false)
    }
  }

  const submitReauth = async (email: string) => {
    setControlling(`reauth-${email}`)
    setMessage(null)
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(email)}/actions/keepalive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auth_mode: "password", force_oauth_reauth: true }),
      })
      const payload = (await response.json()) as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "重新获取授权提交失败")
    } finally {
      setControlling(null)
    }
  }

  const jobRows = Object.values(jobs).sort((left, right) => right.updated_at.localeCompare(left.updated_at))
  const actionRows = Object.values(accountActions)
    .flatMap((states) => Object.values(states))
    .filter((state): state is AccountActionState => state != null && state.action === "keepalive")
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))

  const controlAction = async (state: AccountActionState, command: "pause" | "resume", step?: string) => {
    const controlKey = `${state.email}-${state.action}-${command}-${step || ""}`
    setControlling(controlKey)
    setMessage(null)
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(state.email)}/actions/${state.action}/${command}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step }) })
      const payload = (await response.json()) as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : command === "pause" ? "暂停保活失败" : "继续保活失败")
    } finally {
      setControlling(null)
    }
  }

  return (
    <div className="space-y-6">
      <section className="grid gap-6 xl:grid-cols-2">
        <Card className="p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Registration flow</p>
              <h2 className="mt-1 text-base font-semibold text-slate-900">完全注册</h2>
            </div>
            <ShieldCheck className="h-5 w-5 text-teal-700" />
          </div>
          <div className="mt-5 grid gap-2 sm:grid-cols-2">
            {[
              "注册",
              "填写密保邮箱",
              "获取授权",
              "加入 HX-Email",
            ].map((label) => <StepLine key={label} label={label} />)}
          </div>
          <div className="mt-6 grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-medium text-slate-500">任务数<input type="number" min={1} max={10000} value={registrationCount} onChange={(event) => setRegistrationCount(Number(event.target.value) || 1)} className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
            <label className="text-xs font-medium text-slate-500">并发数<input type="number" min={1} max={64} value={registrationConcurrency} onChange={(event) => setRegistrationConcurrency(Number(event.target.value) || 1)} className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          </div>
          <Button className="mt-5 w-full sm:w-auto" variant="primary" onClick={() => void submitRegistration()} disabled={submitting} title="提交注册批量任务">
            {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            提交注册任务
          </Button>
        </Card>

        <Card className="p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Keepalive flow</p>
              <h2 className="mt-1 text-base font-semibold text-slate-900">完全保活</h2>
            </div>
            <LogIn className="h-5 w-5 text-sky-700" />
          </div>
          <div className="mt-5 grid gap-2 sm:grid-cols-2">
            {[
              "登录",
              "邮箱登录",
              "获取邮箱验证码并提交完成登录",
              "自动解锁安全验证（警告重按 → 长按挑战 → 恢复页面）",
              "获取授权",
              "加入 HX-Email",
            ].map((label) => <StepLine key={label} label={label} />)}
          </div>
          <div className="mt-6 flex flex-wrap gap-2" role="group" aria-label="保活登录方式">
            <Button variant={authMode === "password" ? "primary" : "secondary"} onClick={() => setAuthMode("password")} title="使用账号密码登录">账号密码</Button>
            <Button variant={authMode === "recovery" ? "primary" : "secondary"} onClick={() => setAuthMode("recovery")} title="使用密保邮箱取件登录">密保邮箱取件</Button>
          </div>
          <div className="mt-4">
            <label className="text-xs font-medium text-slate-500" htmlFor="keepalive-target">保活目标账号</label>
            <select
              id="keepalive-target"
              value={keepaliveEmail}
              onChange={(event) => setKeepaliveEmail(event.target.value)}
              className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500"
            >
              <option value="">全部已注册账号（{registeredCount} 个）</option>
              {registeredAccounts.map((account) => (
                <option key={account.email} value={account.email}>{account.email}</option>
              ))}
            </select>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={() => void submitKeepalive()} disabled={submitting || registeredCount === 0} title={keepaliveEmail ? `仅保活 ${keepaliveEmail}` : "提交全部已注册账号的保活任务"}>
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {keepaliveEmail ? "保活所选账号" : "保活已注册账号"}
            </Button>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-600" title="登录通过后强制在浏览器内重新执行 OAuth 授权（获取授权步骤），而不是沿用已有 token">
              <input type="checkbox" checked={forceReauth} onChange={(event) => setForceReauth(event.target.checked)} className="h-4 w-4 accent-teal-700" />
              登录后强制重新获取授权
            </label>
            <span className="text-xs text-slate-400">可处理 {keepaliveEmail ? 1 : registeredCount} 个账号</span>
          </div>
        </Card>
      </section>

      {message && <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800" role="status">{message}</div>}

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Workflow queue</p><h2 className="mt-1 text-base font-semibold text-slate-900">批量任务</h2></div>
          <Button variant="ghost" onClick={() => void refresh()} title="刷新工作流状态" aria-label="刷新工作流状态"><RefreshCw className="h-4 w-4" /></Button>
        </div>
        <div className="divide-y divide-slate-100">
          {jobRows.length === 0 && <p className="px-5 py-10 text-center text-sm text-slate-400">暂无批量任务</p>}
          {jobRows.map((job) => (
            <div key={job.job_id} className="flex flex-col gap-2 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Badge tone={jobTone(job.status)}>{job.status === "queued" ? "排队" : job.status === "running" ? "执行中" : job.status === "succeeded" ? "完成" : "失败"}</Badge><span className="text-sm font-medium text-slate-800">{job.kind === "register" ? "完全注册" : job.kind}</span></div><p className="mt-1 break-words text-xs text-slate-500">{job.message}</p></div>
              <span className="shrink-0 text-xs text-slate-400">{job.total ? `${job.total} 个任务` : ""} {job.concurrency ? `· ${job.concurrency} 并发` : ""}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">Keepalive queue</p><h2 className="mt-1 text-base font-semibold text-slate-900">保活状态</h2></div>
          <Badge tone={actionRows.some((state) => state.status === "manual_verification_required" || state.status === "pausing" || state.status === "paused") ? "warning" : "neutral"}>{actionRows.length} 个账号</Badge>
        </div>
        <div className="divide-y divide-slate-100">
          {actionRows.length === 0 && <p className="px-5 py-10 text-center text-sm text-slate-400">暂无保活任务</p>}
          {actionRows.map((state) => (
            <div key={`${state.email}-${state.action}`} className="px-5 py-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><Badge tone={actionTone(state.status)}>{actionLabel(state.status)}</Badge><span className="break-all text-sm font-medium text-slate-800">{state.email}</span></div>
                  <div className="mt-2 flex items-center gap-2 text-xs text-slate-600"><span className="font-medium text-slate-800">当前步骤</span><span>{stepLabel(state.step)}</span></div>
                  <p className="mt-1 break-words text-xs text-slate-500">{state.message}</p>
                  {(state.status === "manual_verification_required" || state.status === "failed") && <p className={cn("mt-1 text-xs font-medium", state.status === "manual_verification_required" ? "text-amber-600" : "text-red-600")}>浏览器已保留，不会自动关闭</p>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {(state.status === "running" || state.status === "queued") && <Button variant="secondary" onClick={() => void controlAction(state, "pause", state.step)} disabled={controlling !== null} title="请求暂停保活自动化"><Pause className="h-4 w-4" />暂停当前步骤</Button>}
                  {(state.status === "pausing" || state.status === "paused" || state.status === "manual_verification_required") && <Button variant="primary" onClick={() => void controlAction(state, "resume", state.step || "manual_challenge")} disabled={controlling !== null} title="从当前步骤继续自动化"><Play className="h-4 w-4" />继续当前步骤</Button>}
                  {(state.status === "succeeded" || state.status === "failed" || state.status === "paused" || state.status === "manual_verification_required") && <Button variant="secondary" onClick={() => void submitReauth(state.email)} disabled={controlling !== null} title="重新登录后在浏览器内执行 OAuth 授权并加入 HX-Email"><KeyRound className="h-4 w-4" />获取授权</Button>}
                </div>
              </div>
              <div className="mt-4 border-t border-slate-100 pt-3">
                <div className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-500"><ScrollText className="h-3.5 w-3.5" />运行日志</div>
                <div className="scrollbar-thin max-h-36 space-y-1 overflow-y-auto" aria-label={`${state.email} 保活日志`}>
                  {(state.logs || []).length === 0 && <p className="text-xs text-slate-400">暂无日志</p>}
                  {(state.logs || []).slice().reverse().map((entry, index) => (
                    <div key={`${entry.timestamp}-${index}`} className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-2 text-xs leading-5">
                      <span className="tabular-nums text-slate-400">{logTime(entry.timestamp)}</span>
                      <span className={cn("break-words", entry.level === "error" ? "text-red-700" : entry.level === "warning" ? "text-amber-700" : "text-slate-600")}>{entry.message}</span>
                    </div>
                  ))}
                  {state.page_record && <details className="mt-3 border-t border-slate-100 pt-3"><summary className="cursor-pointer text-xs font-medium text-slate-600">人工页面记录</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-4 text-slate-500">{state.page_record.body_text}</pre></details>}
                </div>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
