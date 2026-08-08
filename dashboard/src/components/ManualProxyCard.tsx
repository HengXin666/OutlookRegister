import { KeyRound, ListChecks, LoaderCircle, RotateCcw, Trash2 } from "lucide-react"
import { Badge, Button, Card } from "./ui"

const PLACEHOLDER = "http://账号:密码@1.2.3.4:8000\nhttp://账号:密码@5.6.7.8:8000"

function countLines(value: string) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#")).length
}

export function ManualProxyCard({
  pending,
  used,
  active,
  busy,
  checking,
  onPendingChange,
  onCheck,
  onRecycle,
}: {
  pending: string
  used: string
  active: boolean
  busy: boolean
  checking: boolean
  onPendingChange: (value: string) => void
  onCheck: () => void
  onRecycle: (action: "restore" | "clear") => void
}) {
  const pendingCount = countLines(pending)
  const usedCount = countLines(used)

  return (
    <Card className={active ? "p-5" : "p-5 opacity-60"}>
      <div className="flex items-center justify-between gap-2 text-slate-900">
        <div className="flex items-center gap-2">
          <ListChecks className="h-4 w-4 text-teal-700" />
          <h2 className="font-semibold">手动代理列表</h2>
        </div>
        <Badge tone={active ? "teal" : "neutral"}>{active ? "使用中" : "未启用"}</Badge>
      </div>

      <label className="mt-5 block text-xs font-medium text-slate-500">
        待用代理（每行一个，用完自动移入回收框）
        <textarea
          rows={6}
          spellCheck={false}
          value={pending}
          onChange={(event) => onPendingChange(event.target.value)}
          placeholder={PLACEHOLDER}
          className="mt-1 w-full rounded-md border border-slate-200 px-3 py-2 font-mono text-xs text-slate-800 outline-none focus:border-teal-500"
        />
      </label>
      <p className="mt-1 text-xs text-slate-500">待用 {pendingCount} 行 · 已回收 {usedCount} 行</p>

      <Button
        variant="primary"
        className="mt-4 w-full sm:w-auto"
        onClick={onCheck}
        disabled={busy || pendingCount === 0}
        title="校验第一行代理并启用手动代理来源"
      >
        <KeyRound className="h-4 w-4" />
        {checking ? (
          <>
            <LoaderCircle className="h-4 w-4 animate-spin" />
            正在校验
          </>
        ) : (
          "校验并启用"
        )}
      </Button>

      <div className="mt-5 border-t border-slate-100 pt-4">
        <p className="text-xs font-medium text-slate-500">已使用（回收）</p>
        <textarea
          rows={4}
          readOnly
          value={used}
          placeholder="尚无已使用的代理"
          aria-label="已使用的代理"
          className="mt-1 w-full rounded-md border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-xs text-slate-500 outline-none"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            onClick={() => onRecycle("restore")}
            disabled={busy || usedCount === 0}
            title="把回收框中的代理全部退回待用"
          >
            <RotateCcw className="h-4 w-4" />
            全部退回待用
          </Button>
          <Button
            onClick={() => onRecycle("clear")}
            disabled={busy || usedCount === 0}
            title="清空回收框"
          >
            <Trash2 className="h-4 w-4" />
            清空回收
          </Button>
        </div>
      </div>
    </Card>
  )
}
