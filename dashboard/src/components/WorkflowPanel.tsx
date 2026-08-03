import { useCallback, useEffect, useMemo, useState } from "react"
import { CheckCircle2, LoaderCircle, LogIn, Play, RefreshCw, ShieldCheck } from "lucide-react"
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
  status: "queued" | "running" | "succeeded" | "failed" | "manual_verification_required"
  message: string
  updated_at: string
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
  if (status === "manual_verification_required") return "warning" as const
  return status === "running" ? "teal" as const : "neutral" as const
}

function actionLabel(status: AccountActionState["status"]) {
  if (status === "succeeded") return "完成"
  if (status === "failed") return "失败"
  if (status === "manual_verification_required") return "等待人工验证"
  return status === "running" ? "执行中" : "排队"
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
  const [accountActions, setAccountActions] = useState<AccountActionMap>({})
  const [submitting, setSubmitting] = useState(false)
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

  const registeredCount = useMemo(() => accounts.filter((account) => account.stages.registered.ok).length, [accounts])

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
        body: JSON.stringify({ auth_mode: authMode }),
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

  const jobRows = Object.values(jobs).sort((left, right) => right.updated_at.localeCompare(left.updated_at))
  const actionRows = Object.values(accountActions)
    .flatMap((states) => Object.values(states))
    .filter((state): state is AccountActionState => state != null && state.action === "keepalive")
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))

  const resumeAction = async (state: AccountActionState) => {
    setMessage(null)
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(state.email)}/actions/${state.action}/resume`, { method: "POST" })
      const payload = (await response.json()) as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "继续保活失败")
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
              "登录（账号密码 / 密保邮箱取件）",
              "人工完成按压验证（如有）",
              "补充获取授权（如有）",
              "加入 HX-Email（如有）",
            ].map((label) => <StepLine key={label} label={label} />)}
          </div>
          <div className="mt-6 flex flex-wrap gap-2" role="group" aria-label="保活登录方式">
            <Button variant={authMode === "password" ? "primary" : "secondary"} onClick={() => setAuthMode("password")} title="使用账号密码登录">账号密码</Button>
            <Button variant={authMode === "recovery" ? "primary" : "secondary"} onClick={() => setAuthMode("recovery")} title="使用密保邮箱取件登录">密保邮箱取件</Button>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={() => void submitKeepalive()} disabled={submitting || registeredCount === 0} title="提交全部已注册账号的保活任务">
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              保活已注册账号
            </Button>
            <span className="text-xs text-slate-400">可处理 {registeredCount} 个账号</span>
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
          <Badge tone={actionRows.some((state) => state.status === "manual_verification_required") ? "warning" : "neutral"}>{actionRows.length} 个账号</Badge>
        </div>
        <div className="divide-y divide-slate-100">
          {actionRows.length === 0 && <p className="px-5 py-10 text-center text-sm text-slate-400">暂无保活任务</p>}
          {actionRows.map((state) => (
            <div key={`${state.email}-${state.action}`} className="flex flex-col gap-2 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Badge tone={actionTone(state.status)}>{actionLabel(state.status)}</Badge><span className="break-all text-sm font-medium text-slate-800">{state.email}</span></div><p className="mt-1 break-words text-xs text-slate-500">{state.message}</p></div>
              {state.status === "manual_verification_required" && <Button variant="secondary" className="shrink-0" onClick={() => void resumeAction(state)} title="人工验证完成后继续"><Play className="h-4 w-4" />继续</Button>}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
