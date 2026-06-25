import { useEffect, useRef, useState } from 'react'
import { ApiCallError, get, type ApiOptions } from '@/lib/api'

export interface UseApiState<T> {
  data: T | null
  error: ApiCallError | null
  loading: boolean
  refresh: () => Promise<void>
}

/**
 * Generic GET hook with optional polling.
 *
 *   const { data, loading, error, refresh } = useApi('/admin/api/stats', { pollMs: 5000 })
 *
 * The hook cancels in-flight requests on unmount or when the path
 * changes. When `pollMs > 0`, it re-fetches on that interval.
 */
export function useApi<T = unknown>(
  path: string | null,
  options: ApiOptions & { pollMs?: number } = {},
): UseApiState<T> {
  const { pollMs = 0, ...apiOpts } = options
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiCallError | null>(null)
  const [loading, setLoading] = useState(false)
  const acRef = useRef<AbortController | null>(null)
  const reqIdRef = useRef(0)

  const refresh = async () => {
    if (!path) return
    const myReqId = ++reqIdRef.current
    acRef.current?.abort()
    const ac = new AbortController()
    acRef.current = ac
    setLoading(true)
    try {
      const result = await get<T>(path, { ...apiOpts, signal: ac.signal })
      if (myReqId === reqIdRef.current) {
        setData(result)
        setError(null)
      }
    } catch (e) {
      if (ac.signal.aborted) return
      if (myReqId === reqIdRef.current) {
        setError(e instanceof ApiCallError ? e : new ApiCallError(0, String(e)))
      }
    } finally {
      if (myReqId === reqIdRef.current) setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, pollMs])

  useEffect(() => {
    if (!pollMs || !path) return
    const t = setInterval(refresh, pollMs)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, pollMs])

  useEffect(() => {
    return () => acRef.current?.abort()
  }, [])

  return { data, error, loading, refresh }
}
