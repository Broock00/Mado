/**
 * Application shell.
 *
 * Spec 11.03 requires navigation to answer "where am I / where can I go / how do
 * I get back" at every moment, and to stay consistently positioned. Desktop gets
 * a persistent top bar; mobile gets a bottom tab bar reachable one-handed.
 */

import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { Bookmark, Compass, PenSquare, Search as SearchIcon, User } from 'lucide-react'
import { useSession } from '@/app/hooks'
import { useAppStore } from '@/app/store'
import { ConciergeLauncher, ConciergePanel } from '@/features/concierge/ConciergePanel'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/', label: 'Discover', icon: Compass, end: true },
  { to: '/search', label: 'Search', icon: SearchIcon, end: false },
  { to: '/saved', label: 'Saved', icon: Bookmark, end: false },
  // Publishing is a peer of discovery, not a separate console: a publisher is
  // just an explorer who posts.
  { to: '/posts', label: 'Your posts', icon: PenSquare, end: false },
]

export function AppShell() {
  useSession()
  const user = useAppStore((s) => s.user)
  const signOut = useAppStore((s) => s.signOut)
  const location = useLocation()

  return (
    <div className="min-h-dvh bg-sand-50">
      {/* Skip link: spec 11.07 requires keyboard-only operation to be practical,
          not merely possible. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:shadow-lifted"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-30 border-b border-sand-200 bg-sand-50/85 backdrop-blur">
        <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
          <Link to="/" className="flex items-center gap-2">
            <span className="grid size-8 place-items-center rounded-lg bg-brand-700 text-white">
              <Compass className="size-4.5" aria-hidden />
            </span>
            <span className="text-lg font-semibold tracking-tight text-sand-900">Mado</span>
          </Link>

          <nav aria-label="Primary" className="hidden items-center gap-1 sm:flex">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-brand-100 text-brand-800'
                      : 'text-sand-600 hover:bg-sand-200/60 hover:text-sand-900',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            {user ? (
              <div className="flex items-center gap-2">
                <span className="hidden text-sm text-sand-600 sm:inline">
                  {user.profile.displayName}
                </span>
                <button
                  type="button"
                  onClick={signOut}
                  className="rounded-lg px-3 py-2 text-sm font-medium text-sand-600 hover:bg-sand-200/60"
                >
                  Sign out
                </button>
              </div>
            ) : (
              <Link
                to="/signin"
                state={{ from: location.pathname }}
                className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium text-sand-700 hover:bg-sand-200/60"
              >
                <User className="size-4" aria-hidden />
                Sign in
              </Link>
            )}
          </div>
        </div>
      </header>

      <main id="main">
        <Outlet />
      </main>

      {/* Bottom tabs on mobile: spec 11.03 requires primary navigation to stay
          within one-handed reach. */}
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-30 border-t border-sand-200 bg-white/95 backdrop-blur sm:hidden"
      >
        <div className="flex items-stretch justify-around">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex flex-1 flex-col items-center gap-0.5 py-2.5 text-xs font-medium transition-colors',
                  isActive ? 'text-brand-700' : 'text-sand-500',
                )
              }
            >
              <item.icon className="size-5" aria-hidden />
              {item.label}
            </NavLink>
          ))}
        </div>
      </nav>

      <ConciergeLauncher />
      <ConciergePanel />
    </div>
  )
}
