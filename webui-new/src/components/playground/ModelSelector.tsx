import { useEffect, useState } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useApi } from '@/hooks/useApi'
import { ApiCallError } from '@/lib/api'
import type { ModelsResponse } from '@/lib/types'

interface Props {
  value: string
  onChange: (v: string) => void
}

export function ModelSelector({ value, onChange }: Props) {
  const { data, error } = useApi<ModelsResponse>('/v1/models')
  const [firstRender, setFirstRender] = useState(true)

  // Pick the first model as default if nothing is set yet.
  useEffect(() => {
    if (firstRender && !value && data?.data && data.data.length > 0) {
      onChange(data.data[0].id)
      setFirstRender(false)
    }
  }, [data, value, onChange, firstRender])

  if (error) {
    return (
      <div className="text-xs text-destructive">
        无法加载模型列表（需要先在 <code className="font-mono">.env</code> 设置 <code className="font-mono">API_KEYS</code>）:{' '}
        {error instanceof ApiCallError ? error.message : String(error)}
      </div>
    )
  }

  const models = data?.data ?? []

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="选择模型…" />
      </SelectTrigger>
      <SelectContent>
        {models.map((m) => (
          <SelectItem key={m.id} value={m.id}>
            {m.id}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
