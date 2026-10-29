import * as React from "react"
import { cn } from "@/lib/utils"

export interface ButtonProps
    extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    asChild?: boolean
    variant?: "default" | "destructive" | "outline" | "secondary" | "ghost" | "link"
    size?: "default" | "sm" | "lg" | "icon"
}

// Variant fills match the pre-existing hardcoded CTA conventions this component replaces
// (AUDIT.md 2026-07-03 "wax and polish" pass) — same look, routed through one source of truth:
//   default     = bg-primary text-white (the 79-file convention; the 11-file bg-primary
//                 text-black variant is retired — 2.66:1 light-mode contrast, WCAG-AA fail)
//   destructive = bg-dangerFill text-white (the "danger CTA" convention; dangerFill is the
//                 fill-role red — the dark-theme danger token is a TEXT color and only hit
//                 2.72:1 under white text, same fill/text split as primary/primaryText)
//   secondary   = bg-surfaceAlt text-text border (no raw "secondary" token exists in tailwind.config;
//                 this mirrors the neutral/outline secondary buttons already in use)
//   outline     = bordered, transparent fill
//   ghost       = no fill, hover surface tint
//   link        = text-only, underline on hover
const VARIANT_CLASSES: Record<NonNullable<ButtonProps["variant"]>, string> = {
    // Brand CTA — emerald gradient, matches the landing page .btn-primary exactly
    default: "bg-gradient-to-br from-success to-successDeep text-white shadow-sm hover:shadow-md hover:brightness-105",
    destructive: "bg-dangerFill text-white hover:bg-dangerFill/90",
    secondary: "bg-surfaceAlt text-text border border-border hover:bg-border",
    outline: "border border-border bg-transparent text-text hover:bg-surfaceAlt",
    ghost: "bg-transparent text-text hover:bg-surfaceAlt",
    link: "bg-transparent text-primaryText underline-offset-4 hover:underline",
}

const SIZE_CLASSES: Record<NonNullable<ButtonProps["size"]>, string> = {
    default: "h-10 px-4 py-2",
    sm: "h-9 px-3",
    lg: "h-11 px-8",
    icon: "h-10 w-10",
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
    ({ className, variant = "default", size = "default", asChild = false, ...props }, ref) => {
        return (
            <button
                className={cn(
                    "inline-flex items-center justify-center gap-2 rounded-lg text-sm font-medium ring-offset-surface transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
                    VARIANT_CLASSES[variant],
                    SIZE_CLASSES[size],
                    className
                )}
                ref={ref}
                {...props}
            />
        )
    }
)
Button.displayName = "Button"

export { Button }
