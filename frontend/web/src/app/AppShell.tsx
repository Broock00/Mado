/**
 * Application shell.
 *
 * Spec 11.03 requires navigation to answer "where am I / where can I go / how do
 * I get back" at every moment, and to stay consistently positioned. Desktop gets
 * a persistent top bar; mobile gets a bottom tab bar reachable one-handed.
 */

import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  Bookmark,
  Compass,
  PenSquare,
  Route,
  Search as SearchIcon,
  Settings,
  Library,
  ShieldCheck,
  Ticket,
  User,
} from 'lucide-react'
import { useSession } from '@/app/hooks'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { ConciergeLauncher, ConciergePanel } from '@/features/concierge/ConciergePanel'
import { NotificationBell } from '@/features/notifications/NotificationBell'
import { useLanguage } from '@/app/language-context'
import { useIsOnline } from '@/app/offline'
import { cn } from '@/lib/utils'

// Labels are message keys rather than words. The nav is rendered on every
// screen, so it is the one place where an untranslated string is guaranteed to
// be seen by everybody.
const NAV = [
  { to: '/', key: 'nav.discover', icon: Compass, end: true },
  { to: '/search', key: 'nav.search', icon: SearchIcon, end: false },
  { to: '/plans', key: 'nav.plan', icon: Route, end: false },
  { to: '/saved', key: 'nav.saved', icon: Bookmark, end: false },
  { to: '/collections', key: 'nav.lists', icon: Library, end: false },
  // Publishing is a peer of discovery, not a separate console: a publisher is
  // just an explorer who posts.
  { to: '/posts', key: 'nav.posts', icon: PenSquare, end: false },
]
// Five is the practical ceiling for the mobile tab bar; anything beyond it goes
// in the account area rather than shrinking every target below a comfortable
// tap (spec 11.03).

export function AppShell() {
  useSession()
  const user = useAppStore((s) => s.user)
  const signOut = useAppStore((s) => s.signOut)

  // Which name this account goes by. Fetched rather than read off `/me`,
  // because the business lives in the publisher domain and identity does not
  // reference it - `/me/account-type` is the endpoint that joins the two.
  // Cached hard: it changes at most once in an account's life.
  const { data: accountType } = useQuery({
    queryKey: ['account-type'],
    queryFn: () => api.accountType(),
    enabled: Boolean(user),
    staleTime: 60 * 60_000,
  })

  const accountName =
    accountType?.accountType === 'business' && accountType.business
      ? accountType.business.name
      : user?.profile.displayName

  // Business accounts have a public profile; individuals do not, so the name
  // falls back to the account hub rather than going nowhere.
  const profileTo =
    accountType?.accountType === 'business' && accountType.business?.slug
      ? `/businesses/${accountType.business.slug}`
      : accountType
        ? '/settings'
        : null

  const location = useLocation()
  const { t } = useLanguage()
  const online = useIsOnline()

  return (
    <div className="min-h-dvh bg-sand-50">
      {/* Skip link: spec 11.07 requires keyboard-only operation to be practical,
          not merely possible. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-sand-100 focus:px-4 focus:py-2 focus:text-sand-900 focus:shadow-lifted"
      >
        Skip to content
      </a>

      {/* Said as "cannot reach Mado" rather than "you are offline": the
          browser reports whether there is a network interface, not whether
          anything is reachable, and a captive portal reports a happy one. */}
      {!online && (
        <div
          role="status"
          className="bg-sand-100 px-4 py-2 text-center text-sm text-sand-700"
        >
          {t('offline.banner')}
        </div>
      )}

      <header className="sticky top-0 z-30 border-b border-sand-200 bg-sand-50/90 backdrop-blur">
        <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
          <Link to="/" className="flex items-center gap-2" aria-label="Mado home">
            <img
              src="/logo.png"
              alt="Mado"
              className="size-9 rounded-xl object-cover"
            />
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
                      ? 'bg-sand-200 text-brand-700'
                      : 'text-sand-500 hover:bg-sand-200/70 hover:text-sand-900',
                  )
                }
              >
                {t(item.key)}
              </NavLink>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            {user ? (
              <div className="flex items-center gap-1">
                <NotificationBell />
                <NavLink
                  to="/orders"
                  aria-label={t('nav.orders')}
                  className={({ isActive }) =>
                    cn(
                      'rounded-lg p-2 transition-colors',
                      isActive
                        ? 'bg-sand-200 text-brand-700'
                        : 'text-sand-500 hover:bg-sand-200/70 hover:text-sand-900',
                    )
                  }
                >
                  <Ticket className="size-4.5" aria-hidden />
                </NavLink>
                {/* A display hint only - /moderation re-checks server-side, so a
                    forged flag reveals an empty page and nothing else. */}
                {user.isModerator && (
                  <NavLink
                    to="/moderation"
                    aria-label={t('nav.moderation')}
                    className={({ isActive }) =>
                      cn(
                        'rounded-lg p-2 transition-colors',
                        isActive
                          ? 'bg-sand-200 text-brand-700'
                          : 'text-sand-500 hover:bg-sand-200/70 hover:text-sand-900',
                      )
                    }
                  >
                    <ShieldCheck className="size-4.5" aria-hidden />
                  </NavLink>
                )}
                <NavLink
                  to="/settings"
                  aria-label={t('nav.settings')}
                  className={({ isActive }) =>
                    cn(
                      'rounded-lg p-2 transition-colors',
                      isActive
                        ? 'bg-sand-200 text-brand-700'
                        : 'text-sand-500 hover:bg-sand-200/70 hover:text-sand-900',
                    )
                  }
                >
                  <Settings className="size-4.5" aria-hidden />
                </NavLink>
                {/* A business account *is* the business, so the header names
                    the business rather than the person who signed in. */}
                {profileTo && accountName ? (
                  <Link
                    to={profileTo}
                    className="hidden max-w-[12rem] truncate rounded-lg pl-1 text-sm text-sand-500 transition-colors hover:bg-sand-200/70 hover:text-sand-900 sm:inline"
                  >
                    {accountName}
                  </Link>
                ) : accountName ? (
                  <span className="hidden max-w-[12rem] truncate pl-1 text-sm text-sand-500 sm:inline">
                    {accountName}
                  </span>
                ) : null}
                <button
                  type="button"
                  onClick={signOut}
                  className="rounded-lg px-3 py-2 text-sm font-medium text-sand-500 transition-colors hover:bg-sand-200/70 hover:text-sand-900"
                >
                  {t('account.signOut')}
                </button>
              </div>
            ) : (
              <Link
                to="/signin"
                state={{ from: location.pathname }}
                className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium text-sand-600 transition-colors hover:bg-sand-200/70 hover:text-sand-900"
              >
                <User className="size-4" aria-hidden />
                {t('account.signIn')}
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
        className="fixed inset-x-0 bottom-0 z-30 border-t border-sand-200 bg-sand-50/95 backdrop-blur sm:hidden"
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
              {t(item.key)}
            </NavLink>
          ))}
        </div>
      </nav>

      <ConciergeLauncher />
      <ConciergePanel />
    </div>
  )
}
