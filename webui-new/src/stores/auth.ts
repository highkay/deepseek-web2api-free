import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  setToken: (token: string) => void
  logout: () => void
}

/**
 * Persists the admin bearer token in localStorage so a page reload keeps
 * the user signed in. The token is opaque; the server validates it on
 * every request.
 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      setToken: (token) => set({ token }),
      logout: () => set({ token: null }),
    }),
    { name: 'ds2api-auth' },
  ),
)
