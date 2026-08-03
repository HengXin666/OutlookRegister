import { useCallback, useEffect, useMemo, useState } from "react"
import { AlertCircle, Check, LoaderCircle, Plus, RefreshCw, Save, Settings2, Trash2 } from "lucide-react"
import { Badge, Button, Card } from "./ui"

type ConfigValue = string | number | boolean | null | ConfigValue[] | { [key: string]: ConfigValue }
type ConfigResponse = {
  revision: string
  config: Record<string, ConfigValue>
  validation_errors: string[]
  runtime_validation_errors: string[]
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

function inputValue(value: ConfigValue) {
  return typeof value === "string" && value === CONFIGURED_VALUE ? "已配置（留空保持）" : String(value ?? "")
}

function objectValue(value: ConfigValue): Record<string, ConfigValue> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, ConfigValue>
    : {}
}

function configuredCountryPool(draft: Record<string, ConfigValue>) {
  const configured = getPath(draft, ["identity", "country_pool"], null)
  if (Array.isArray(configured)) return configured
  const identity = objectValue(getPath(draft, ["identity"], {}))
  return [{
    country_code: identity.country_code || "",
    browser_locale: identity.browser_locale || identity.locale || "",
    timezone: identity.timezone || "",
  }]
}

export function ConfigPanel() {
  const [payload, setPayload] = useState<ConfigResponse | null>(null)
  const [draft, setDraft] = useState<Record<string, ConfigValue> | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async (preserveDraft = false) => {
    const response = await fetch("/api/config", { cache: "no-store" })
    const next = (await response.json()) as ConfigResponse
    if (!response.ok) throw new Error((next as unknown as { detail?: string }).detail || `HTTP ${response.status}`)
    setPayload(next)
    if (!preserveDraft) setDraft(next.config)
    setLoading(false)
  }, [])

  useEffect(() => {
    void load().catch((reason) => { setError(reason instanceof Error ? reason.message : "无法读取配置"); setLoading(false) })
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

  const errors = useMemo(() => payload?.runtime_validation_errors || [], [payload])
  if (loading && !draft) return <div className="flex items-center gap-2 text-sm text-slate-500"><LoaderCircle className="h-4 w-4 animate-spin" />正在读取配置</div>
  if (!draft) return <Card className="p-6"><div className="flex items-center gap-2 text-red-700"><AlertCircle className="h-5 w-5" />{error || "配置不可用"}</div></Card>

  const proxyRotation = (getPath(draft, ["proxy_rotation"], {}) as Record<string, ConfigValue>) || {}
  const tokens = Array.isArray(proxyRotation.tokens) ? proxyRotation.tokens : []
  const countryPool = configuredCountryPool(draft)

  const updateCountryProfile = (index: number, key: string, value: ConfigValue) => {
    const next = countryPool.map((entry) => ({ ...objectValue(entry) }))
    next[index] = { ...next[index], [key]: value }
    update(["identity", "country_pool"], next)
  }

  const addCountryProfile = () => {
    update(["identity", "country_pool"], [
      ...countryPool,
      { country_code: "", browser_locale: "", timezone: "" },
    ])
  }

  const removeCountryProfile = (index: number) => {
    if (countryPool.length <= 1) return
    update(
      ["identity", "country_pool"],
      countryPool.filter((_entry, entryIndex) => entryIndex !== index),
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-teal-700">Runtime configuration</p><h1 className="mt-1 text-xl font-semibold text-slate-950">运行配置</h1><p className="mt-1 text-sm text-slate-500">版本 {payload?.revision || "未记录"} · 保存后对新任务热加载</p></div>
        <div className="flex items-center gap-2"><Badge tone={dirty ? "warning" : "success"}>{dirty ? "有未保存修改" : "已同步"}</Badge><Button variant="ghost" onClick={() => void load()} title="重新读取配置" aria-label="重新读取配置"><RefreshCw className="h-4 w-4" /></Button><Button variant="primary" onClick={() => void save()} disabled={saving || !dirty} title="保存配置"><Save className="h-4 w-4" />保存</Button></div>
      </div>

      {error && <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700" role="alert">{error}</div>}
      {notice && <div className="rounded-md border border-teal-200 bg-teal-50 px-4 py-3 text-sm text-teal-800" role="status"><Check className="mr-2 inline h-4 w-4" />{notice}</div>}
      {errors.length > 0 && <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><div className="flex items-center gap-2 font-medium"><AlertCircle className="h-4 w-4" />当前配置不能启动动态住宅 IP 任务</div><ul className="mt-2 space-y-1 pl-5 text-xs">{errors.map((item) => <li key={item} className="list-disc">{item}</li>)}</ul></div>}

      <section className="grid gap-6 xl:grid-cols-2">
        <Card className="p-5">
          <div className="flex items-center gap-2 text-slate-900"><Settings2 className="h-4 w-4 text-teal-700" /><h2 className="font-semibold">国家与浏览器</h2></div>
          <label className="mt-5 block text-xs font-medium text-slate-500">国家选择<select value={String(getPath(draft, ["identity", "country_selection"], "random"))} onChange={(event) => update(["identity", "country_selection"], event.target.value)} className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500"><option value="random">每个 flow 随机选择</option></select></label>
          <div className="mt-5 space-y-3">
            {countryPool.map((entry, index) => {
              const profile = objectValue(entry)
              return <div key={index} className="border-b border-slate-100 pb-3 last:border-b-0 last:pb-0">
                <div className="mb-2 flex items-center justify-between"><span className="text-xs font-semibold text-slate-600">国家 {index + 1}</span><Button variant="ghost" className="h-7 w-7 px-0 text-slate-500 hover:text-red-700" onClick={() => removeCountryProfile(index)} disabled={countryPool.length <= 1} title="删除国家" aria-label={`删除国家 ${index + 1}`}><Trash2 className="h-3.5 w-3.5" /></Button></div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <label className="text-xs font-medium text-slate-500">国家代码<input value={String(profile.country_code || "")} onChange={(event) => updateCountryProfile(index, "country_code", event.target.value.toUpperCase())} placeholder="US" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
                  <label className="text-xs font-medium text-slate-500">浏览器语言<input value={String(profile.browser_locale || profile.locale || "")} onChange={(event) => updateCountryProfile(index, "browser_locale", event.target.value)} placeholder="en-US" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
                  <label className="text-xs font-medium text-slate-500">时区<input value={String(profile.timezone || "")} onChange={(event) => updateCountryProfile(index, "timezone", event.target.value)} placeholder="America/New_York" className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
                </div>
              </div>
            })}
          </div>
          <Button variant="secondary" className="mt-4" onClick={addCountryProfile} title="添加国家"><Plus className="h-4 w-4" />添加国家</Button>
          <label className="mt-5 flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["identity", "require_dynamic_residential_ip"], true))} onChange={(event) => update(["identity", "require_dynamic_residential_ip"], event.target.checked)} className="h-4 w-4 accent-teal-700" />强制动态住宅 IP</label>
        </Card>

        <Card className="p-5">
          <h2 className="font-semibold text-slate-900">任务并发</h2>
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
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["keepalive", "verify_existing_oauth_token"], true))} onChange={(event) => update(["keepalive", "verify_existing_oauth_token"], event.target.checked)} className="h-4 w-4 accent-teal-700" />保活时实际探针已有 refresh token</label>
          <label className="flex items-center gap-3 text-sm text-slate-700"><input type="checkbox" checked={Boolean(getPath(draft, ["keepalive", "auto_import_hx_email"], true))} onChange={(event) => update(["keepalive", "auto_import_hx_email"], event.target.checked)} className="h-4 w-4 accent-teal-700" />有可用授权时自动加入 HX-Email</label>
        </div>
      </Card>

      <Card className="p-5">
        <h2 className="font-semibold text-slate-900">HX-ProxyGroup 住宅会话</h2>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-medium text-slate-500 sm:col-span-2">控制面地址<input value={String(getPath(draft, ["proxy_rotation", "base_url"]))} onChange={(event) => update(["proxy_rotation", "base_url"], event.target.value)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <label className="text-xs font-medium text-slate-500 sm:col-span-2">出口 IP 校验地址<input value={String(getPath(draft, ["proxy_rotation", "exit_ip_endpoint"]))} onChange={(event) => update(["proxy_rotation", "exit_ip_endpoint"], event.target.value)} className="mt-1 h-9 w-full rounded-md border border-slate-200 px-3 text-sm text-slate-800 outline-none focus:border-teal-500" /></label>
          <label className="text-xs font-medium text-slate-500">完成后路由<select value={String(getPath(draft, ["proxy_rotation", "post_registration_route"], "residential"))} onChange={(event) => update(["proxy_rotation", "post_registration_route"], event.target.value)} className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500"><option value="residential">保持住宅会话</option><option value="upstream" disabled>上游代理（严格模式禁止）</option><option value="direct" disabled>直连（严格模式禁止）</option></select></label>
          <label className="text-xs font-medium text-slate-500">国家回显<select value={String(getPath(draft, ["proxy_rotation", "require_country_echo"], false))} onChange={(event) => update(["proxy_rotation", "require_country_echo"], event.target.value === "true")} className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-800 outline-none focus:border-teal-500"><option value="true">必须回显</option><option value="false">兼容旧协议</option></select></label>
        </div>
        <div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {(["enabled", "session_scoped", "check_proxy", "enforce_unique_exit_ip", "verify_browser_exit_ip"] as const).map((key) => <label key={key} className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={Boolean(proxyRotation[key])} onChange={(event) => update(["proxy_rotation", key], event.target.checked)} className="h-4 w-4 accent-teal-700" />{key}</label>)}
        </div>
        <div className="mt-6 overflow-x-auto rounded-md border border-slate-200"><table className="w-full min-w-[620px] text-left text-xs"><thead className="border-b border-slate-200 bg-slate-50 text-slate-500"><tr><th className="px-3 py-2">渠道 token</th><th className="px-3 py-2">Listener</th><th className="px-3 py-2">国家</th></tr></thead><tbody className="divide-y divide-slate-100">{tokens.map((entry, index) => { const item = (entry && typeof entry === "object" && !Array.isArray(entry) ? entry : {}) as Record<string, ConfigValue>; return <tr key={index}><td className="px-3 py-2"><input value={inputValue(item.token || "")} onChange={(event) => { const next = JSON.parse(JSON.stringify(tokens)) as ConfigValue[]; (next[index] as Record<string, ConfigValue>).token = event.target.value === "已配置（留空保持）" ? CONFIGURED_VALUE : event.target.value; update(["proxy_rotation", "tokens"], next) }} className="h-8 w-full rounded border border-slate-200 px-2 text-slate-700" /></td><td className="px-3 py-2"><input value={inputValue(item.proxy || "")} onChange={(event) => { const next = JSON.parse(JSON.stringify(tokens)) as ConfigValue[]; (next[index] as Record<string, ConfigValue>).proxy = event.target.value === "已配置（留空保持）" ? CONFIGURED_VALUE : event.target.value; update(["proxy_rotation", "tokens"], next) }} className="h-8 w-full rounded border border-slate-200 px-2 text-slate-700" /></td><td className="px-3 py-2"><input value={String(item.country_code || "")} onChange={(event) => { const next = JSON.parse(JSON.stringify(tokens)) as ConfigValue[]; (next[index] as Record<string, ConfigValue>).country_code = event.target.value.toUpperCase(); update(["proxy_rotation", "tokens"], next) }} className="h-8 w-24 rounded border border-slate-200 px-2 text-slate-700" placeholder="US" /></td></tr> })}</tbody></table></div>
      </Card>
    </div>
  )
}
