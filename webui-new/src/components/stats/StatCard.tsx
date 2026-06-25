import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import type { LucideIcon } from 'lucide-react'

interface StatCardProps {
  label: string
  value: string | number
  hint?: string
  icon?: LucideIcon
  tone?: 'default' | 'primary' | 'success' | 'warning' | 'destructive'
  loading?: boolean
}

const toneClass: Record<NonNullable<StatCardProps['tone']>, string> = {
  default: 'text-foreground',
  primary: 'text-primary',
  success: 'text-success',
  warning: 'text-warning',
  destructive: 'text-destructive',
}

export function StatCard({ label, value, hint, icon: Icon, tone = 'default', loading }: StatCardProps) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {label}
          </div>
          {Icon && <Icon className="h-4 w-4 text-muted-foreground/60" />}
        </div>
        <div className={cn('mt-2 text-2xl font-bold tabular-nums', toneClass[tone])}>
          {loading ? '—' : value}
        </div>
        {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  )
}
