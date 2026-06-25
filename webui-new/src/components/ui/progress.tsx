import * as React from 'react'
import { cn } from '@/lib/utils'

export interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value?: number
  max?: number
  indicatorClassName?: string
}

export function Progress({ className, value = 0, max = 100, indicatorClassName, ...props }: ProgressProps) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100))
  return (
    <div
      className={cn('relative h-2 w-full overflow-hidden rounded-full bg-secondary', className)}
      role="progressbar"
      aria-valuenow={value}
      aria-valuemax={max}
      {...props}
    >
      <div
        className={cn('h-full bg-primary transition-all', indicatorClassName)}
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
