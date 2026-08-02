import { useEffect, useRef } from 'react'
import { User, Bot } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

interface Props {
  messages: ChatMessage[]
  streaming?: string
}

export function MessageList({ messages, streaming }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, streaming])

  if (messages.length === 0 && !streaming) {
    return (
      <Card className="flex h-full items-center justify-center border-dashed">
        <CardContent className="text-center text-sm text-muted-foreground py-12">
          <Bot className="mx-auto h-8 w-8 mb-2 opacity-40 text-primary" />
          <p>在左侧输入消息并点击"发送"</p>
          <p className="mt-1 text-xs">需要 .env 中配置 <code className="font-mono">API_KEYS</code></p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-3">
      {messages.map((m, i) => (
        <div
          key={i}
          className={cn('flex gap-3', m.role === 'user' ? 'justify-end' : 'justify-start')}
        >
          {m.role !== 'user' && (
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/15 text-primary">
              <Bot className="h-3.5 w-3.5" />
            </div>
          )}
          <div
            className={cn(
              'max-w-[80%] rounded-lg border px-3 py-2 text-sm shadow-sm',
              m.role === 'user'
                ? 'bg-gradient-to-br from-primary/15 to-primary/5 border-primary/25'
                : 'bg-card',
            )}
          >
            <div className="whitespace-pre-wrap break-words">{m.content || <span className="text-muted-foreground italic">（空）</span>}</div>
          </div>
          {m.role === 'user' && (
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-secondary text-secondary-foreground">
              <User className="h-3.5 w-3.5" />
            </div>
          )}
        </div>
      ))}
      {streaming !== undefined && (
        <div className="flex gap-3 justify-start">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/15 text-primary">
            <Bot className="h-3.5 w-3.5" />
          </div>
          <div className="max-w-[80%] rounded-lg border bg-card px-3 py-2 text-sm shadow-sm">
            <div className="whitespace-pre-wrap break-words">
              {streaming}
              <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-primary align-middle" />
            </div>
          </div>
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
}
