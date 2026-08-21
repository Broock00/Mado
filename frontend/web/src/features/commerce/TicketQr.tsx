/**
 * The scannable half of a ticket.
 *
 * **The code goes in the QR and nothing else.** Putting the holder's name, the
 * tier and the event into the square is tempting - it reads like "all the
 * information" - and it is worse in both directions. Anybody who photographs
 * the screen over somebody's shoulder can read it, and a square that carries
 * its own claims is either signed or forgeable, so a scanner would be trusting
 * the thing being checked. The code is an opaque, random, unique identifier;
 * the door looks it up and the database answers. That is also what makes a
 * revoked ticket stop working the moment it is revoked rather than whenever the
 * printed square happens to be reprinted.
 *
 * Rendered as SVG rather than canvas so it stays sharp when somebody zooms in
 * on a cracked phone in a dark queue, and so printing it works.
 */

import { useEffect, useState } from 'react'
import QRCode from 'qrcode'

export function TicketQr({ code, size = 148 }: { code: string; size?: number }) {
  const [svg, setSvg] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let current = true
    QRCode.toString(code, {
      type: 'svg',
      margin: 1,
      // Level M survives a scuffed screen and a bit of glare without making the
      // square so dense that a poor camera cannot resolve it.
      errorCorrectionLevel: 'M',
      color: { dark: '#1c1917', light: '#ffffff' },
    })
      .then((markup) => {
        if (current) setSvg(markup)
      })
      .catch(() => {
        if (current) setFailed(true)
      })
    return () => {
      current = false
    }
  }, [code])

  // The printed code underneath is not decoration. A camera that will not focus,
  // a cracked lens, a scanner app that has crashed - the door can still type
  // this in, and somebody gets into the thing they paid for.
  return (
    <figure className="flex flex-col items-center gap-1.5">
      {svg && !failed ? (
        <div
          className="rounded-lg bg-sand-100 p-2 shadow-sm"
          style={{ width: size, height: size }}
          // The markup is produced locally from a code this client already
          // holds; nothing here came from a server response.
          dangerouslySetInnerHTML={{ __html: svg }}
          role="img"
          aria-label={`Ticket code ${code}`}
        />
      ) : (
        <div
          className="grid place-items-center rounded-lg border border-dashed border-sand-300 p-2 text-center text-xs text-sand-500"
          style={{ width: size, height: size }}
        >
          {failed ? 'Show the code below at the door' : 'Loading…'}
        </div>
      )}
      <figcaption className="font-mono text-xs tracking-widest text-sand-700">{code}</figcaption>
    </figure>
  )
}
