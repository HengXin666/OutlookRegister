import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Globe2,
  KeyRound,
  LoaderCircle,
  Mail,
  RefreshCw,
  Save,
  Settings2,
} from "lucide-react"
import { Badge, Button, Card } from "./ui"
import { ManualProxyCard } from "./ManualProxyCard"

const RESIDENTIAL_SOURCE = "residential"
const MANUAL_SOURCE = "manual"

type ConfigValue = string | number | boolean | null | ConfigValue[] | { [key: string]: ConfigValue }
type ConfigResponse = {
  revision: string
  config: Record<string, ConfigValue>
  validation_errors: string[]
  runtime_validation_errors: string[]
}
type ProxyCheckResponse = ConfigResponse & {
  ok?: boolean
  exit_ip?: string
  country_code?: string
  browser_locale?: string
  timezone?: string
  pending?: number
  detail?: string
}

const CONFIGURED_VALUE = "__configured__"

function getPath(source: Record<string, ConfigValue>, path: string[], fallback: ConfigValue = "") {
  let value: ConfigValue = source
  for (const key of path) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return fallback
    value = (value as Record<string, ConfigValue>)[key]
  }
  return value ?? fallback
}

function setPath(source: Record<string, ConfigValue>, path: string[], value: ConfigValue) {
  const result = JSON.parse(JSON.stringify(source)) as Record<string, ConfigValue>
  let target = result
  path.slice(0, -1).forEach((key) => {
    const current = target[key]
    if (!current || typeof current !== "object" || Array.isArray(current)) target[key] = {}
    target = target[key] as Record<string, ConfigValue>
  })
  target[path[path.length - 1]] = value
  return result
}

function isConfigured(value: ConfigValue) {
  return typeof value === "string" && value === CONFIGURED_VALUE
}

function inputValue(value: ConfigValue) {
  return isConfigured(value) ? "已配置（输入新 URL 替换）" : String(value ?? "")
}

function linesFrom(value: ConfigValue) {
  if (Array.isArray(value)) return value.map((item) => String(item ?? "")).join("\n")
  return String(value ?? "")
}

function toLines(value: string) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
}

export function ConfigPanel() {
  const [payload, setPayload] = useState<ConfigResponse | null>(null)
  const [draft, setDraft] = useState<Record<string, ConfigValue> | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [checking, setChecking] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingText, setPendingText] = useState("")

  const load = useCallback(async () => {
    const response = await fetch("/api/config", { cache: "no-store" })
    const next = (await response.json()) as ConfigResponse & { detail?: string }
    if (!response.ok) throw new Error(next.detail || `HTTP ${response.status}`)
    setPayload(next)
    setDraft(next.config)
    setPendingText(linesFrom(getPath(next.config, ["manual_proxy_pool", "pending"], [])))
    setLoading(false)
  }, [])

  useEffect(() => {
    void load().catch((reason) => {
      setError(reason instanceof Error ? reason.message : "无法读取配置")
      setLoading(false)
    })
    const source = new EventSource("/api/config/stream")
    source.addEventListener("config", () => {
      if (!dirty) void load().catch(() => undefined)
      else setNotice("配置已在其他窗口更新，当前编辑仍保留")
    })
    return () => source.close()
  }, [dirty, load])

  const update = (path: string[], value: ConfigValue) => {
    setDraft((current) => current ? setPath(current, path, value) : current)
    setDirty(true)
    setNotice(null)
    setError(null)
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setError(null)
    try {
      const response = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch: draft }),
      })
      const result = (await response.json()) as ConfigResponse & { detail?: string }
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`)
      setPayload(result)
      setDraft(result.config)
      setDirty(false)
      setNotice("配置已保存，新任务会读取最新版本")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "配置保存失败")
    } finally {
      setSaving(false)
    }
  }

  const checkAndEnable = async () => {
    if (!draft) return
    const value = getPath(draft, ["proxy_rotation", "control_url"])
    const controlUrl = String(value || "").trim()
    if (!controlUrl || isConfigured(value)) {
      setError("请粘贴完整的 HX-ProxyGroup 住宅控制 URL")
      return
    }
    setChecking(true)
    setError(null)
    setNotice(null)
    try {
      let response: Response
      try {
        response = await fetch("/api/proxy-rotation/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ control_url: controlUrl }),
        })
      } catch {
        throw new Error("本地仪表盘后端连接中断，请确认服务仍在运行后重试")
      }
      let result: ProxyCheckResponse
      try {
        result = (await response.json()) as ProxyCheckResponse
      } catch {
        throw new Error(`本地仪表盘后端返回了无效响应（HTTP ${response.status}）`)
      }
      if (!response.ok || !result.config) {
        throw new Error(result.detail || `住宅代理校验失败（HTTP ${response.status}）`)
      }
      setPayload(result)
      setDraft(result.config)
      setDirty(false)
      setNotice(
        `校验通过：${result.country_code || "未知国家"} · ${result.browser_locale || "en-US"} · ${result.timezone || "UTC"} · 出口 ${result.exit_ip || "已确认"}`,
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "住宅代理校验失败")
    } finally {
      setChecking(false)
    }
  }

  const applyResult = (result: ProxyCheckResponse) => {
    setPayload(result)
    setDraft(result.config)
    setPendingText(linesFrom(getPath(result.config, ["manual_proxy_pool", "pending"], [])))
    setDirty(false)
  }

  const postJson = async (url: string, body: unknown, failureLabel: string) => {
    let response: Response
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
    } catch {
      throw new Error("本地仪表盘后端连接中断，请确认服务仍在运行后重试")
    }
    let result: ProxyCheckResponse
    try {
      result = (await response.json()) as ProxyCheckResponse
    } catch {
      throw new Error(`本地仪表盘后端返回了无效响应（HTTP ${response.status}）`)
    }
    if (!response.ok || !result.config) {
      throw new Error(result.detail || `${failureLabel}（HTTP ${response.status}）`)
    }
    return result
  }

  const checkManualProxies = async () => {
    const pending = toLines(pendingText)
    if (pending.length === 0) {
      setError("请至少填写一行代理，例如 http://账号:密码@1.2.3.4:8000")
      return
    }
    setChecking(true)
    setError(null)
    setNotice(null)
    try {
      const result = await postJson("/api/manual-proxy/check", { pending }, "手动代理校验失败")
      applyResult(result)
      setNotice(
        `校验通过：${result.country_code || "未知国家"} · ${result.browser_locale || "en-US"} · ${result.timezone || "UTC"} · 出口 ${result.exit_ip || "已确认"} · 待用 ${result.pending ?? pending.length} 行`,
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "手动代理校验失败")
    } finally {
      setChecking(false)
    }
  }

  const recycleManualProxies = async (action: "restore" | "clear") => {
    setChecking(true)
    setError(null)
    setNotice(null)
    try {
      const result = await postJson("/api/manual-proxy/recycle", { action }, "回收操作失败")
      applyResult(result)
      setNotice(action === "restore" ? "已把回收的代理退回待用" : "已清空回收框")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "回收操作失败")
    } finally {
      setChecking(false)
    }
  }

  const proxySource = draft
    ? String(getPath(draft, ["proxy_source"], RESIDENTIAL_SOURCE) || RESIDENTIAL_SOURCE)
    : RESIDENTIAL_SOURCE
  const manualMode = proxySource === MANUAL_SOURCE

  const errors = useMemo(() => {
    const configuredErrors = payload?.runtime_validation_errors || []
    // The identity fields are derived from the verified exit IP in both
    // sources, so their "missing" errors are noise once a source is set up.
    const identityNoise = (item: string) =>
      !item.includes("identity.country_code") &&
      !item.includes("identity.country_pool") &&
      !item.includes("identity.country_codes")
    if (manualMode) {
      return toLines(pendingText).length > 0
        ? configuredErrors.filter(identityNoise)
        : configuredErrors
    }
    const currentUrl = draft
      ? String(getPath(draft, ["proxy_rotation", "control_url"]) || "").trim()
      : ""
    if (currentUrl && currentUrl !== CONFIGURED_VALUE) {
      return configuredErrors.filter(identityNoise)
    }
    return configuredErrors
  }, [draft, payload, manualMode, pendingText])

  if (loading && !draft) return <div className="flex items-center gap-2 text-sm text-slate-500"><LoaderCircle className="h-4 w-4 animate-spin" />正在读取配置</div>
  if (!draft) return <Card className="p-6"><div className="flex items-center gap-2 text-red-700"><AlertCircle className="h-5 w-5" />{error || "配置不可用"}</div></Card>

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-teal-700">Runtime configuration</p><h1 className="mt-1 text-xl font-semibold text-slate-950">运行配置</h1><p className="mt-1 text-sm text-slate-500">版本 {payload?.revision || "未记录"} · 保存后对新任务热加载</p></div>
        <div className="flex items-center gap-2"><Badge tone={dirty ? "warning" : "success"}>{dirty ? "有未保存修改" : "已同步"}</Badge><Button variant="ghost" onClick={() => void load()} title="重新读取配置" aria-label="重新读取配置"><RefreshCw className="h-4 w-4" /></Button><Button variant="primary" onClick={() => void save()} disabled={saving || checking || !dirty} title="保存配置"><Save className="h-4 w-4" />保存</Button></div>
      </div>

      {error && <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700" role="alert"><AlertCircle className="mr-2 inline h-4 w-4" />{error}</div>}
      {notice && <div className="rounded-md border border-teal-200 bg-teal-50 px-4 py-3 text-sm text-teal-800" role="status"><Check className="mr-2 inline h-4 w-4" />{notice}</div>}
      {errors.length > 0 && <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><div className="flex items-center gap-2 font-medium"><AlertCircle className="h-4 w-4" />当前配置不能启动动态住宅 IP 任务</div><ul className="mt-2 space-y-1 pl-5 text-xs">{errors.map((item) => <li key={item} className="list-disc">{item}</li>)}</ul></div>}

      <Card className="p-5">
        <div className="flex items-center gap-2 text-slate-900"><Globe2 className="h-4 w-4 text-teal-700" /><h2 className="font-semibold">代理来源</h2></div>
        <p className="mt-1 text-xs text-slate-500">每个 flow 只使用一种来源，不会混用。</p>
        <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="代理来源">
          <Button variant={manualMode ? "secondary" : "primary"} onClick={() => update(["proxy_source"], RESIDENTIAL_SOURCE)} title="使用 HX-ProxyGroup 住宅节点池">HX-ProxyGroup 住宅身份</Button>
          <Button variant={manualMode ? "primary" : "secondary"} onClick={() => update(["proxy_source"], MANUAL_SOURCE)} title="使用手动粘贴的代理列表">手动代理列表</Button>
        </div>
      </Card>

      <section className="grid gap-6 xl:grid-cols-2">
        <Card className={manualMode ? "p-5 opacity-60" : "p-5"}>
          <div className="flex items-center justify-between gap-2 text-slate-900">
            <div className="flex items-center gap-2"><Globe2 className="h-4 w-4 text-teal-700" /><h2 className="font-semibold">HX-ProxyGroup 住宅身份</h2></div>
            <Badge tone={manualMode ? "neutral" : "teal"}>{manualMode ? "未启用" : "使用中"}</Badge>
          </div>
          <label className="mt-5 block text-xs font-medium text-slate-500">住宅控制 URL<input type="url" autoComplete="off" spellCheck={false} value={inputValue(getPath(draft, ["proxy_rotation", "control_url"]))} onChange={(event) => update(["proxy_rotation", "control_url"], event.target.value)} placeholder="https://主机/ctl/访问令牌" className="mt-1 h-10 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <Button variant="primary" className="mt-4 w-full sm:w-auto" onClick={() => void checkAndEnable()} disabled={checking || saving} title="校验住宅代理并启用"><KeyRound className="h-4 w-4" />{checking ? <><LoaderCircle className="h-4 w-4 animate-spin" />正在校验</> : "校验并启用"}</Button>
          <div className="mt-5 grid gap-3 border-t border-slate-100 pt-4 sm:grid-cols-3">
            <div><p className="text-xs text-slate-500">国家</p><p className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-slate-800"><CheckCircle2 className="h-4 w-4 text-emerald-600" />自动确认</p></div>
            <div><p className="text-xs text-slate-500">浏览器语言</p><p className="mt-1 text-sm font-semibold text-slate-800">自动匹配</p></div>
            <div><p className="text-xs text-slate-500">时区</p><p className="mt-1 text-sm font-semibold text-slate-800">自动匹配</p></div>
          </div>
        </Card>

        <ManualProxyCard
          pending={pendingText}
          used={linesFrom(getPath(draft, ["manual_proxy_pool", "used"], []))}
          active={manualMode}
          busy={saving || checking}
          checking={checking}
          onPendingChange={(value) => {
            setPendingText(value)
            update(["manual_proxy_pool", "pending"], toLines(value))
          }}
          onCheck={() => void checkManualProxies()}
          onRecycle={(action) => void recycleManualProxies(action)}
        />

        <Card className="p-5">
          <div className="flex items-center gap-2 text-slate-900"><Settings2 className="h-4 w-4 text-teal-700" /><h2 className="font-semibold">任务并发</h2></div>
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-slate-500">并发 flow<input type="number" min={1} max={64} value={Number(getPath(draft, ["concurrent_flows"], 1))} onChange={(event) => update(["concurrent_flows"], Number(event.target.value) || 1)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
            <label className="text-xs font-medium text-slate-500">最大任务数<input type="number" min={1} max={10000} value={Number(getPath(draft, ["max_tasks"], 1))} onChange={(event) => update(["max_tasks"], Number(event.target.value) || 1)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          </div>
        </Card>
      </section>

      <Card className="p-5">
        <div className="flex items-center gap-2 text-slate-900"><Settings2 className="h-4 w-4 text-sky-700" /><h2 className="font-semibold">保活判定</h2></div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-medium text-slate-500">登录超时（秒）<input type="number" min={30} max={900} value={Number(getPath(draft, ["keepalive", "login_timeout_seconds"], 180))} onChange={(event) => update(["keepalive", "login_timeout_seconds"], Number(event.target.value) || 180)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <label className="text-xs font-medium text-slate-500">人工验证等待（秒）<input type="number" min={1} max={3600} value={Number(getPath(draft, ["keepalive", "manual_verification_timeout_seconds"], 300))} onChange={(event) => update(["keepalive", "manual_verification_timeout_seconds"], Number(event.target.value) || 300)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <label className="text-xs font-medium text-slate-500">按压验证自动尝试次数<input type="number" min={1} max={8} value={Number(getPath(draft, ["keepalive", "unlock_press_attempts"], 2))} onChange={(event) => update(["keepalive", "unlock_press_attempts"], Number(event.target.value) || 2)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["keepalive", "auto_unlock_locked_account"], true))} onChange={(event) => update(["keepalive", "auto_unlock_locked_account"], event.target.checked)} className="h-4 w-4 accent-teal-700" />自动处理账号锁定页的按压验证</label>
          <label className="flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["keepalive", "verify_existing_oauth_token"], true))} onChange={(event) => update(["keepalive", "verify_existing_oauth_token"], event.target.checked)} className="h-4 w-4 accent-teal-700" />保活时实际探针已有 refresh token</label>
          <label className="flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["keepalive", "auto_import_hx_email"], true))} onChange={(event) => update(["keepalive", "auto_import_hx_email"], event.target.checked)} className="h-4 w-4 accent-teal-700" />有可用授权时自动加入 HX-Email</label>
        </div>
      </Card>

      <Card className="p-5">
        <div className="flex items-center gap-2 text-slate-900"><Mail className="h-4 w-4 text-sky-700" /><h2 className="font-semibold">HX-Email 分组</h2></div>
        <p className="mt-1 text-xs text-slate-500">保活会先在保活分组内查找该账号：已存在则更新，不存在才新增。分组代理默认 127.0.0.1:2334，绝不使用住宅代理。</p>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-medium text-slate-500">注册账号分组<input type="text" autoComplete="off" spellCheck={false} value={String(getPath(draft, ["recovery_email", "hx_email", "register_account_group"], "") || "")} onChange={(event) => update(["recovery_email", "hx_email", "register_account_group"], event.target.value)} placeholder="OutlookRegister 自动注册" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <label className="text-xs font-medium text-slate-500">保活账号分组<input type="text" autoComplete="off" spellCheck={false} value={String(getPath(draft, ["recovery_email", "hx_email", "keepalive_account_group"], "") || "")} onChange={(event) => update(["recovery_email", "hx_email", "keepalive_account_group"], event.target.value)} placeholder="OutlookRegister 保活" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
        </div>
        <label className="mt-4 block text-xs font-medium text-slate-500">分组代理（HTTP，留空则使用 127.0.0.1:2334）<input type="text" autoComplete="off" spellCheck={false} value={inputValue(getPath(draft, ["recovery_email", "hx_email", "proxy_url"])) || "http://127.0.0.1:2334"} onChange={(event) => update(["recovery_email", "hx_email", "proxy_url"], event.target.value)} placeholder="http://127.0.0.1:2334" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
        <label className="mt-5 flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["isolate_hx_email_group"], false))} onChange={(event) => update(["isolate_hx_email_group"], event.target.checked)} className="h-4 w-4 accent-teal-700" />每个 flow 使用独立分组（仅注册流程追加 flow ID）</label>
      </Card>
    </div>
  )
}
