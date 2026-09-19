/**
 * Notifications in the header.
 *
 * A bell with an unread count, and a panel listing what has been delivered.
 * Deliberately not a separate page: a notification exists to send someone
 * somewhere else, so making them navigate to a destination in order to reach a
 * destination is one hop too many.
 *
 * Polled rather than pushed. A websocket for a handful of reminders a week is
 * infrastructure that has to be operated, monitored and reconnected for almost
 * no benefit - and the delivery cycle is ten minutes, so a minute of polling
 * latency is invisible against it.
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, CalendarClock, CalendarX2, Route, ShieldCheck, Sparkles } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { NotificationEntry } from '@/lib/types'
import { cn } from '@/lib/utils'

const ICONS: Record<string, typeof Bell> = {
  event_reminder: CalendarClock,
  plan_reminder: Route,
  plan_day_reminder: Route,
  moderation_outcome: ShieldCheck,
  nearby_suggestion: Sparkles,
  travel_alert: CalendarX2,
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return ''
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function NotificationBell() {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  const { data } = useQuery({
    queryKey: ['notifications'],
    queryFn: () => api.notifications(),
    enabled: Boolean(user),
    // Comfortably inside the ten-minute delivery cycle without being chatty.
    refetchInterval: 60_000,
  })

  const markRead = useMutation({
    mutationFn: (id: string) => api.markNotificationRead(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })

  const markAllRead = useMutation({
    mutationFn: () => api.markAllNotificationsRead(),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })

  // Close on an outside click or Escape - a panel that traps you is worse than
  // no panel.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!user) return null

  const unread = data?.unread ?? 0
  const notifications = data?.notifications ?? []

  return (
    <div className="relative" ref={panelRef}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
        aria-expanded={open}
        className={cn(
          'relative rounded-lg p-2 transition-colors',
          open
            ? 'bg-brand-900/50 text-brand-300'
            : 'text-sand-600 hover:bg-sand-200/60 hover:text-sand-900',
        )}
      >
        <Bell className="size-4.5" aria-hidden />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-brand-700 px-1 text-[0.65rem] font-semibold text-white">
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-2 w-80 overflow-hidden rounded-xl border border-sand-200 bg-sand-100 shadow-lifted">
          <div className="flex items-center justify-between border-b border-sand-200 px-4 py-2.5">
            <span className="text-sm font-medium text-sand-900">Notifications</span>
            {unread > 0 && (
              <button
                type="button"
                onClick={() => markAllRead.mutate()}
                className="text-xs text-sand-600 hover:text-sand-900"
              >
                Mark all read
              </button>
            )}
          </div>

          {notifications.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-sand-500">
              Nothing yet. Save something happening soon and you will be reminded.
            </p>
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {notifications.map((notification) => (
                <NotificationRow
                  key={notification.id}
                  notification={notification}
                  onOpen={() => {
                    if (notification.isUnread) markRead.mutate(notification.id)
                    setOpen(false)
                  }}
                />
              ))}
            </ul>
          )}

          <Link
            to="/settings"
            onClick={() => setOpen(false)}
            className="block border-t border-sand-200 px-4 py-2.5 text-xs text-sand-600 hover:bg-sand-200"
          >
            Choose what you are told about
          </Link>
        </div>
      )}
    </div>
  )
}

function NotificationRow({
  notification,
  onOpen,
}: {
  notification: NotificationEntry
  onOpen: () => void
}) {
  const Icon = ICONS[notification.kind] ?? Bell
  const body = (
    <div className="flex gap-3">
      <Icon
        className={cn(
          'mt-0.5 size-4 shrink-0',
          notification.isUnread ? 'text-brand-600' : 'text-sand-400',
        )}
        aria-hidden
      />
      <div className="min-w-0">
        <p
          className={cn(
            'text-sm',
            notification.isUnread ? 'font-medium text-sand-900' : 'text-sand-700',
          )}
        >
          {notification.title}
        </p>
        {notification.body && (
          <p className="mt-0.5 text-xs text-sand-600">{notification.body}</p>
        )}
        <p className="mt-0.5 text-xs text-sand-400">{timeAgo(notification.deliveredAt)}</p>
      </div>
    </div>
  )

  return (
    <li className={cn('border-b border-sand-200 last:border-b-0', notification.isUnread && 'bg-brand-900/25')}>
      {notification.link ? (
        <Link to={notification.link} onClick={onOpen} className="block px-4 py-3 hover:bg-sand-200">
          {body}
        </Link>
      ) : (
        <button type="button" onClick={onOpen} className="block w-full px-4 py-3 text-left hover:bg-sand-200">
          {body}
        </button>
      )}
    </li>
  )
}
