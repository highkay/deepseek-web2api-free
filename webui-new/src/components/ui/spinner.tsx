import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface SpinnerProps extends React.HTMLAttributes<HTMLDivElement> {
  size?: 'sm' | 'md' | 'lg'
}

export function Spinner({ className, size = 'md', ...props }: SpinnerProps) {
  const dim = size === 'sm' ? 'h-3 w-3' : size === 'lg' ? 'h-8 w-8' : 'h-5 w-5'
  return (
    <div className={cn('flex items-center justify-center', className)} {...props}>
      <Loader2 className={cn('animate-spin text-muted-foreground', dim)} />
    </div>
  )
}
