/**
 * A social platform's own mark, drawn in the current text colour.
 *
 * The glyph travels with the platform (see `social.ts`) rather than being looked
 * up here, so there is no key that can miss — where these are shown as the icon
 * alone, a missing mark is an invisible link.
 *
 * Always `aria-hidden`: the accessible name belongs to whatever wraps it, which
 * knows whether it is a link to somebody's Instagram or a label for the field
 * that sets one.
 */

import type { SocialPlatform } from './social'

export function SocialIcon({
  platform,
  className = 'size-[1.125rem]',
}: {
  platform: SocialPlatform
  className?: string
}) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden focusable="false">
      <path d={platform.path} />
    </svg>
  )
}
