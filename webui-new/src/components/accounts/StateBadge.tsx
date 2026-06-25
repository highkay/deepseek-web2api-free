import { Badge } from '@/components/ui/badge'
import { stateLabel } from '@/lib/utils'
import type { Account } from '@/lib/types'

export function StateBadge({ state }: { state: Account['state'] }) {
  const variant =
    state === 'idle' ? 'idle' : state === 'busy' ? 'busy' : state === 'error' ? 'error' : 'outline'
  return <Badge variant={variant as 'idle' | 'busy' | 'error' | 'outline'}>{stateLabel(state)}</Badge>
}
