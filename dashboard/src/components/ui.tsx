import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react"
import { cn } from "../lib/utils"

type ButtonVariant = "primary" | "secondary" | "ghost"

export function Button({
  className,
  variant = "secondary",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      className={cn(
        "inline-flex h-9 items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-600/40 disabled:pointer-events-none disabled:opacity-50",
        variant === "primary" && "border-teal-700 bg-teal-700 text-white hover:bg-teal-800",
        variant === "secondary" && "border-slate-200 bg-white text-slate-700 shadow-sm hover:border-teal-300 hover:text-teal-800",
        variant === "ghost" && "border-transparent bg-transparent text-slate-500 hover:bg-slate-100 hover:text-slate-900",
        className,
      )}
      {...props}
    />
  )
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode
  tone?: "success" | "warning" | "danger" | "neutral" | "teal"
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex min-h-6 items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        tone === "success" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        tone === "warning" && "border-amber-200 bg-amber-50 text-amber-800",
        tone === "danger" && "border-red-200 bg-red-50 text-red-700",
        tone === "teal" && "border-teal-200 bg-teal-50 text-teal-800",
        tone === "neutral" && "border-slate-200 bg-slate-50 text-slate-600",
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-lg border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]", className)}
      {...props}
    />
  )
}

export function Progress({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cn("h-1.5 overflow-hidden rounded-full bg-slate-100", className)}>
      <div
        className="h-full rounded-full bg-teal-600 transition-[width] duration-500"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  )
}
