import { Outlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

/**
 * AppShell — sidebar + topbar + scrollable content area. The actual
 * page content is provided by the nested <Outlet />.
 */
export function AppShell() {
  return (
    <div className="flex h-full bg-background">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="mx-auto w-full max-w-7xl p-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
