/**
 * The door, for one date (spec COM-003).
 *
 * Scoped to a single occurrence deliberately. A scanner that admits any ticket
 * for any of a listing's nights is not a door, it is a turnstile with the lock
 * taken out - the person holding next Friday's ticket is exactly who this is
 * supposed to catch.
 *
 * **Manual entry sits beside the camera, not behind a fallback link.** Doors
 * are dark, lenses crack, and permission prompts get dismissed by whoever
 * borrowed the phone. Somebody who paid should get in even when none of the
 * clever part works, so typing the code is a first-class way to use this screen
 * rather than an apology.
 *
 * **The verdict is loud and the reason is specific.** Five outcomes, five
 * colours, five sentences - "invalid" tells the person on the door nothing they
 * can act on, and the difference between a duplicate and a wrong night is the
 * difference between calling security and pointing at tomorrow.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import {
  ArrowLeft,
  Camera,
  CameraOff,
  CircleCheck,
  CircleX,
  Clock,
  Keyboard,
  TriangleAlert,
} from 'lucide-react'

import jsQR from 'jsqr'

import { ApiError, api } from '@/lib/api'
import type { Scan } from '@/lib/types'
import { Badge, Button, Card, Input, SectionHeading } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

const VERDICT: Record<
  string,
  { label: string; detail: string; tone: 'good' | 'warn' | 'bad' }
> = {
  admitted: { label: 'Let them in', detail: 'First scan of this ticket.', tone: 'good' },
  already_admitted: {
    label: 'Already used',
    detail: 'This ticket has been scanned before.',
    tone: 'bad',
  },
  wrong_event: {
    label: 'Wrong date',
    detail: 'A real ticket, but not for this one.',
    tone: 'warn',
  },
  void: { label: 'Not valid', detail: 'This ticket was cancelled or refunded.', tone: 'bad' },
  unknown: { label: 'Not a ticket', detail: 'No ticket has this code.', tone: 'bad' },
}

const TONE = {
  good: 'border-green-300 bg-green-50 text-green-900',
  warn: 'border-accent-300 bg-accent-100/60 text-accent-800',
  bad: 'border-red-300 bg-red-50 text-red-900',
}

/** Present on Android and ChromeOS, absent on desktop Chrome for Windows -
 *  checked in the browser rather than assumed, because assuming it is how the
 *  camera half of a door scanner ships broken for whoever is actually running
 *  the door. Used when it is there because it is hardware-accelerated, and
 *  jsQR decodes the frame when it is not. */
type DetectedBarcode = { rawValue: string }
type BarcodeDetectorLike = { detect: (source: CanvasImageSource) => Promise<DetectedBarcode[]> }
declare global {
  interface Window {
    BarcodeDetector?: new (options?: { formats: string[] }) => BarcodeDetectorLike
  }
}

export function ScannerPage() {
  const { experienceId, occurrenceId } = useParams<{
    experienceId: string
    occurrenceId: string
  }>()

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [scanning, setScanning] = useState(false)
  const [cameraError, setCameraError] = useState<string | null>(null)
  const [typed, setTyped] = useState('')
  const [result, setResult] = useState<Scan | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The last code sent, so a camera holding steady on one square does not fire
  // the same scan sixty times and report "already used" against itself.
  const lastSent = useRef<string | null>(null)

  const check = useMutation({
    mutationFn: (code: string) => api.scanTicket(experienceId!, occurrenceId!, code),
    onSuccess: (scan) => {
      setResult(scan)
      setError(null)
      setTyped('')
    },
    onError: (caught) => {
      setError(caught instanceof ApiError ? caught.message : 'That did not work.')
      lastSent.current = null
    },
  })

  const submit = useCallback(
    (code: string) => {
      const cleaned = code.trim()
      if (!cleaned || cleaned === lastSent.current || check.isPending) return
      lastSent.current = cleaned
      check.mutate(cleaned)
    },
    [check],
  )

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    setScanning(false)
  }, [])

  const start = useCallback(async () => {
    setCameraError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        // The back camera on a phone, which is the one pointed at a ticket.
        video: { facingMode: 'environment' },
      })
      streamRef.current = stream
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }
      setScanning(true)
    } catch {
      setCameraError(
        'No camera, or permission was refused. Type the code underneath it instead.',
      )
    }
  }, [])

  // Stop the camera when this screen goes away. A door phone left with its
  // light on flattens before the second set finishes.
  useEffect(() => stop, [stop])

  useEffect(() => {
    if (!scanning) return
    const detector = window.BarcodeDetector
      ? new window.BarcodeDetector({ formats: ['qr_code'] })
      : null
    const canvas = document.createElement('canvas')
    let alive = true

    const tick = async () => {
      const video = videoRef.current
      if (!alive || !video || video.readyState < 2) return
      try {
        if (detector) {
          const found = await detector.detect(video)
          if (found.length > 0) submit(found[0].rawValue)
          return
        }
        // No native decoder, so read the pixels ourselves. Downscaled to a
        // fixed width: a 1080p frame is several times more work per tick than
        // jsQR needs to find a square held up to the lens.
        const width = 480
        const height = Math.round((video.videoHeight / video.videoWidth) * width) || 360
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d', { willReadFrequently: true })
        if (!context) return
        context.drawImage(video, 0, 0, width, height)
        const found = jsQR(context.getImageData(0, 0, width, height).data, width, height, {
          inversionAttempts: 'dontInvert',
        })
        if (found?.data) submit(found.data)
      } catch {
        // A frame that could not be read is not an error worth showing; the
        // next one is a third of a second away.
      }
    }
    const timer = setInterval(tick, 300)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [scanning, submit])

  const verdict = result ? (VERDICT[result.verdict] ?? VERDICT.unknown) : null

  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-6 sm:px-6">
      <Link
        to={`/posts/${experienceId}/bookings`}
        className="inline-flex items-center gap-1.5 text-sm text-sand-600 hover:text-sand-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Back to bookings
      </Link>

      <div className="mt-3">
        <SectionHeading
          title="At the door"
          subtitle="Only tickets for this date will be let in."
        />
      </div>

      {result && verdict && (
        <Card
          className={cn('mt-4 border-2 p-5', TONE[verdict.tone])}
          role="status"
          aria-live="assertive"
        >
          <div className="flex items-start gap-3">
            {verdict.tone === 'good' ? (
              <CircleCheck className="mt-0.5 size-7 shrink-0" aria-hidden />
            ) : verdict.tone === 'warn' ? (
              <TriangleAlert className="mt-0.5 size-7 shrink-0" aria-hidden />
            ) : (
              <CircleX className="mt-0.5 size-7 shrink-0" aria-hidden />
            )}
            <div className="min-w-0">
              <p className="text-xl font-semibold">{verdict.label}</p>
              <p className="text-sm opacity-90">{verdict.detail}</p>

              {result.name && (
                <p className="mt-2 text-sm">
                  <span className="font-medium">{result.name}</span>
                  {result.ticketTypeName && ` · ${result.ticketTypeName}`}
                  {result.reference && (
                    <span className="block font-mono text-xs opacity-70">
                      {result.reference}
                    </span>
                  )}
                </p>
              )}

              {result.verdict === 'already_admitted' && result.checkedInAt && (
                <p className="mt-1 flex items-center gap-1.5 text-sm">
                  <Clock className="size-4" aria-hidden />
                  First scanned {new Date(result.checkedInAt).toLocaleTimeString()}
                </p>
              )}
            </div>
          </div>

          <p className="mt-3 text-sm opacity-80">
            {result.admittedCount} of {result.issuedCount} in
          </p>
        </Card>
      )}

      <Card className="mt-4 space-y-3 p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 text-sm font-medium text-sand-700">
            <Camera className="size-4" aria-hidden />
            Scan
          </h2>
          {scanning ? (
            <Button variant="secondary" size="sm" onClick={stop}>
              <CameraOff className="size-4" aria-hidden />
              Stop the camera
            </Button>
          ) : (
            <Button variant="secondary" size="sm" onClick={() => void start()}>
              <Camera className="size-4" aria-hidden />
              Use the camera
            </Button>
          )}
        </div>

        <div className={cn('overflow-hidden rounded-xl bg-sand-900', !scanning && 'hidden')}>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption -- a live camera
              feed has nothing to caption. */}
          <video ref={videoRef} className="aspect-video w-full object-cover" muted playsInline />
        </div>

        {cameraError && <p className="text-sm text-sand-600">{cameraError}</p>}

        <div className="border-t border-sand-200 pt-3">
          <label
            htmlFor="code"
            className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-sand-700"
          >
            <Keyboard className="size-4" aria-hidden />
            Or type the code
          </label>
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              lastSent.current = null
              submit(typed)
            }}
          >
            <Input
              id="code"
              value={typed}
              onChange={(e) => setTyped(e.target.value.toUpperCase())}
              placeholder="The code printed under the square"
              autoComplete="off"
              className="font-mono tracking-widest"
            />
            <Button type="submit" loading={check.isPending} disabled={!typed.trim()}>
              Check
            </Button>
          </form>
          {error && <p className="mt-2 text-sm text-danger">{error}</p>}
        </div>
      </Card>

      <p className="mt-3 text-xs text-sand-500">
        Every scan is recorded, so a ticket that has already been used says so and
        who it belongs to. Looking a code up on the bookings page does not use it.
      </p>

      {result && (
        <Button
          variant="ghost"
          className="mt-3"
          onClick={() => {
            setResult(null)
            lastSent.current = null
          }}
        >
          Next person
        </Button>
      )}

      {check.isPending && <Badge tone="neutral">Checking…</Badge>}
    </div>
  )
}
