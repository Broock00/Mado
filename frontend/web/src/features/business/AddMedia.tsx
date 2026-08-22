/**
 * The one way media gets into a business gallery.
 *
 * Its own component because there are two places somebody reasonably reaches
 * for it — the manage dashboard, where the rest of the editing is, and the
 * gallery tab on the profile, where they are actually looking at the pictures.
 * Two copies of an uploader would be two things to keep in step, and the one
 * that drifts is always the one used less.
 *
 * Uploads run one at a time even when several files are picked. The limit and
 * the ordering are both decided server-side per request, so firing six in
 * parallel makes the order they land in a race, and a batch that trips the
 * gallery limit halfway would leave the client unable to say which of the six
 * failed.
 */

import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ImagePlus, Loader2 } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import { Button } from '@/design-system/primitives'
import type { ButtonSize, ButtonVariant } from '@/design-system/button-styles'

// What the file picker offers. Matches what the server accepts rather than
// `image/*,video/*`: offering a `.mov` and then refusing it wastes the upload of
// a file that is usually the largest thing somebody will send us all day.
const ACCEPTS = 'image/jpeg,image/png,image/webp,image/heic,image/heif,video/mp4,video/webm'

/** What an uploader is allowed to send, in a sentence. Shown next to the button. */
export const MEDIA_HINT =
  'JPEG, PNG or WebP images, and MP4 or WebM video. Videos up to 100 MB.'

/**
 * What to say when the server refuses on authorization rather than on the file.
 *
 * The button is offered to everybody and the server decides, so these are
 * ordinary answers here rather than bugs. They need rewording: the gate returns
 * "Business not found" to somebody with no relationship to it — deliberately, so
 * that a stranger cannot probe which businesses exist — and that reads as broken
 * to a person looking straight at the page it was served from.
 */
function refusal(cause: ApiError): string {
  switch (cause.code) {
    case 'PUBLISHER_NOT_FOUND':
    case 'INSUFFICIENT_BUSINESS_PERMISSION':
      return 'Only this business can add photos and videos here.'
    // `AuthenticationError` in app/core/errors.py, which is what an expired or
    // absent session reaches this endpoint as.
    case 'AUTHENTICATION_REQUIRED':
      return 'Sign in as this business to add photos and videos.'
    default:
      return cause.message
  }
}

export function AddMedia({
  businessId,
  slug,
  label = 'Add photos or videos',
  variant,
  size = 'sm',
}: {
  businessId: string
  /**
   * The public profile's cache key. The profile holds its own copy of the
   * gallery, fetched with the business, so an upload that only invalidated the
   * manage-side list would leave the owner's own profile stale until something
   * else happened to refetch it.
   */
  slug?: string
  label?: string
  variant?: ButtonVariant
  size?: ButtonSize
}) {
  const queryClient = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  // Which of a multi-file pick is in flight, so the button can say "3 of 7"
  // instead of spinning silently through a hundred megabytes of video.
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)

  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      setProgress({ done: 0, total: files.length })
      for (const [index, file] of files.entries()) {
        setProgress({ done: index, total: files.length })
        await api.uploadBusinessMedia(businessId, file)
      }
    },
    onSuccess: () => setError(null),
    onError: (cause) => {
      // Named, not swallowed. The refusals here are all things the uploader can
      // act on — too large, wrong format, gallery full — and "upload failed"
      // throws away the only part that was useful.
      setError(cause instanceof ApiError ? refusal(cause) : 'That file could not be uploaded.')
    },
    onSettled: () => {
      setProgress(null)
      // Invalidated whether or not it threw: a batch that failed on its fourth
      // file still stored three, and leaving the list stale would make them look
      // lost until a reload.
      void queryClient.invalidateQueries({ queryKey: ['business-gallery', businessId] })
      if (slug) void queryClient.invalidateQueries({ queryKey: ['business', slug] })
    },
  })

  return (
    <div className="space-y-2">
      <Button
        size={size}
        variant={variant}
        disabled={upload.isPending}
        onClick={() => input.current?.click()}
      >
        {upload.isPending ? (
          <>
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {progress && progress.total > 1
              ? `Uploading ${progress.done + 1} of ${progress.total}…`
              : 'Uploading…'}
          </>
        ) : (
          <>
            <ImagePlus className="size-4" aria-hidden />
            {label}
          </>
        )}
      </Button>

      {/* Hidden, and clicked by the button above rather than styled directly: a
          file input cannot be restyled to match the rest of the interface, and
          the label-wrapping trick loses the disabled state while an upload is
          already running. */}
      <input
        ref={input}
        type="file"
        accept={ACCEPTS}
        multiple
        className="hidden"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          // Cleared before the upload rather than after: the value is what the
          // browser compares against, so picking the same file twice in a row
          // fires no change event at all if it is still sitting there.
          event.target.value = ''
          if (files.length > 0) upload.mutate(files)
        }}
      />

      {error && (
        <p role="alert" className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </p>
      )}
    </div>
  )
}
