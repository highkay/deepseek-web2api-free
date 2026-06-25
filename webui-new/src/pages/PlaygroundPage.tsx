import { useState, useRef, useEffect } from 'react'
import { Send, Square, Trash2, FlaskConical } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useToast } from '@/hooks/use-toast'
import { useAuthStore } from '@/stores/auth'
import { ApiCallError } from '@/lib/api'
import { ModelSelector } from '@/components/playground/ModelSelector'
import { MessageList, type ChatMessage } from '@/components/playground/MessageList'

interface ApiMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export default function PlaygroundPage() {
  const token = useAuthStore((s) => s.token)
  const { toast } = useToast()

  const [model, setModel] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [temperature, setTemperature] = useState(0.7)
  const [thinking, setThinking] = useState(false)
  const [search, setSearch] = useState(false)
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const acRef = useRef<AbortController | null>(null)

  useEffect(() => () => acRef.current?.abort(), [])

  const handleSend = async () => {
    if (!input.trim() || !model || busy) return
    const userMsg: ChatMessage = { role: 'user', content: input }
    const nextHistory = [...history, userMsg]
    setHistory(nextHistory)
    setInput('')
    setBusy(true)
    setStreaming('')

    const apiMessages: ApiMessage[] = []
    if (systemPrompt.trim()) {
      apiMessages.push({ role: 'system', content: systemPrompt })
    }
    for (const m of nextHistory) apiMessages.push({ role: m.role, content: m.content })

    const ac = new AbortController()
    acRef.current = ac

    try {
      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model,
          messages: apiMessages,
          temperature,
          stream: true,
          thinking_mode: thinking,
          search_enabled: search,
        }),
        signal: ac.signal,
      })

      if (!res.ok || !res.body) {
        const body = await res.text()
        throw new ApiCallError(res.status, `HTTP ${res.status}: ${body.slice(0, 300)}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let acc = ''

      // Parse SSE: events separated by "\n\n", each "data: <json>" line.
      // "data: [DONE]" terminates.
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })

        let sep
        while ((sep = buf.indexOf('\n\n')) !== -1) {
          const event = buf.slice(0, sep)
          buf = buf.slice(sep + 2)
          for (const line of event.split('\n')) {
            const trimmed = line.trim()
            if (!trimmed.startsWith('data:')) continue
            const payload = trimmed.slice(5).trim()
            if (payload === '[DONE]') continue
            try {
              const obj = JSON.parse(payload)
              const choice = obj.choices?.[0]
              if (!choice) continue
              const delta = choice.delta ?? {}
              if (delta.reasoning_content) {
                acc += delta.reasoning_content
              } else if (delta.content) {
                acc += delta.content
              }
              setStreaming(acc)
            } catch {
              // ignore malformed chunk
            }
          }
        }
      }

      // Finalize: move streaming into history.
      setHistory([...nextHistory, { role: 'assistant', content: acc }])
      setStreaming(undefined)
    } catch (e) {
      if (ac.signal.aborted) {
        setHistory([...nextHistory, { role: 'assistant', content: streaming ?? '' }])
      } else {
        const msg = e instanceof ApiCallError ? e.message : String(e)
        toast({ title: '请求失败', description: msg, variant: 'destructive' })
      }
      setStreaming(undefined)
    } finally {
      setBusy(false)
      acRef.current = null
    }
  }

  const handleStop = () => {
    acRef.current?.abort()
  }

  const handleClear = () => {
    if (history.length === 0) return
    if (!confirm('清空所有对话？')) return
    setHistory([])
    setStreaming(undefined)
  }

  return (
    <div className="space-y-4 animate-fade-in h-full flex flex-col">
      <PageHeader
        title="Playground"
        description="在线测试 /v1/chat/completions（需要 .env 配置 API_KEYS）"
        actions={
          <Button variant="outline" onClick={handleClear} disabled={history.length === 0 || busy}>
            <Trash2 className="h-4 w-4" />
            清空
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[280px,1fr] flex-1 min-h-0">
        {/* Left column: parameters */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              <FlaskConical className="h-4 w-4" />
              参数
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>模型</Label>
              <ModelSelector value={model} onChange={setModel} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="system">System 提示</Label>
              <textarea
                id="system"
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                placeholder="可选，例如 'You are a helpful assistant.'"
                className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                rows={3}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="temp">温度: {temperature.toFixed(1)}</Label>
              <input
                id="temp"
                type="range"
                min="0"
                max="2"
                step="0.1"
                value={temperature}
                onChange={(e) => setTemperature(parseFloat(e.target.value))}
                className="w-full"
              />
            </div>

            <div className="flex items-center justify-between">
              <Label htmlFor="thinking">专家模式</Label>
              <Switch id="thinking" checked={thinking} onCheckedChange={setThinking} />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="search">联网搜索</Label>
              <Switch id="search" checked={search} onCheckedChange={setSearch} />
            </div>
          </CardContent>
        </Card>

        {/* Right column: conversation */}
        <Card className="flex flex-col min-h-0">
          <CardContent className="flex-1 min-h-0 overflow-y-auto scrollbar-thin p-4">
            <MessageList messages={history} streaming={streaming} />
          </CardContent>
          <div className="border-t p-3 flex items-end gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSend()
                }
              }}
              placeholder="输入消息，回车发送，Shift+回车换行…"
              disabled={busy || !model}
              className="flex-1"
            />
            {busy ? (
              <Button variant="destructive" onClick={handleStop} aria-label="停止">
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button onClick={handleSend} disabled={!input.trim() || !model}>
                <Send className="h-4 w-4" />
                发送
              </Button>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}
