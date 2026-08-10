/**
 * English messages. The reference catalogue.
 *
 * Every other language is checked against this one, so a key added here without
 * a translation is visible rather than silently English (see `untranslated`).
 *
 * Keys are `area.thing`, and a message is always a whole sentence or a whole
 * label - never a fragment for a caller to concatenate. Sentences assembled
 * from pieces are assembled in English word order, and Amharic puts the verb
 * last, so the pieces cannot be reordered by any amount of care at the call
 * site.
 *
 * Counted messages are suffixed by CLDR plural category. English uses `one` and
 * `other`; Amharic puts zero in `one`; nothing here should assume either.
 */

export const en = {
  // Navigation
  'nav.discover': 'Discover',
  'nav.search': 'Search',
  'nav.plan': 'Plan',
  'nav.saved': 'Saved',
  'nav.lists': 'Lists',
  'nav.posts': 'Posts',
  'nav.moderation': 'Moderation',
  'nav.settings': 'Privacy and data',
  'nav.notifications': 'Notifications',

  // Account
  'account.signIn': 'Sign in',
  'account.signOut': 'Sign out',

  // Common actions
  'action.save': 'Save',
  'action.cancel': 'Cancel',
  'action.retry': 'Try again',
  'action.close': 'Close',
  'action.seeAll': 'See all',

  // Discovery
  'discover.title': 'Discover your city',
  'discover.loading': 'Finding things for you…',
  'discover.empty.title': 'Nothing here yet',
  'discover.empty.body': 'Try a different city, or come back a little later.',
  'discover.openNow': 'Open now',
  'discover.startingSoon': 'Starting soon',
  'discover.free': 'Free',

  // Search
  'search.placeholder': 'Coffee, live music, somewhere quiet…',
  'search.submit': 'Search',
  'search.empty.title': 'Nothing matched',
  'search.empty.body': 'Try fewer words, or a different part of the city.',
  'search.results.one': '{count} result',
  'search.results.other': '{count} results',

  // Saving
  'saved.add': 'Save',
  'saved.remove': 'Saved',
  'saved.signInFirst': 'Sign in to save things.',

  // Reservations
  'reserve.hold': 'Hold a place',
  'reserve.holding': 'Holding…',
  'reserve.cancel': 'Cancel',
  'reserve.nothingToPay': 'Nothing to pay now - this just holds your place.',
  'reserve.remaining.one': 'Only {count} place left',
  'reserve.remaining.other': 'Only {count} places left',
  'reserve.full': 'Fully booked',
  'reserve.cancelled': 'Cancelled',

  // Planning
  'plan.stops.one': '{count} stop',
  'plan.stops.other': '{count} stops',
  'plan.travel': '{minutes} min of travel',
  'plan.startNavigating': 'Start navigating',
  'plan.stopNavigating': 'Stop navigating',
  'plan.positionStaysHere': 'Your position stays on this device - Mado is not told where you are.',

  // Settings
  'settings.title': 'Privacy and data',
  'settings.language.title': 'Language',
  'settings.language.subtitle': 'What Mado uses for its own words. Listings stay as their author wrote them.',
  'settings.language.saved': 'Saved',

  // Voice and photo search (spec SRCH-004, SRCH-005)
  'search.voice.start': 'Search by speaking',
  'search.voice.stop': 'Stop listening',
  'search.voice.listening': 'Listening… speak now.',
  'search.voice.denied': 'Mado cannot hear the microphone. Allow it in your browser settings.',
  'search.voice.unsupportedLanguage':
    'Speaking a search is not available in Amharic yet - browsers cannot transcribe it.',
  'search.visual.button': 'Search with a photo',
  'search.visual.tooBig': 'That photo is too large. Try one under 12 MB.',
  'search.visual.looksLike': 'Looks like: {description}',
  'search.visual.notIdentification':
    'This finds places like the photo, not the exact place in it.',
  'search.visual.unclear': 'Could not make out that photo.',
  'search.visual.unclear.detail': 'Try a clearer, closer shot of the place itself.',

  // Offline (spec EXP-005)
  'offline.banner': 'Cannot reach Mado. Plans you kept are still here.',
  'offline.savedCopy': 'Showing your saved copy. Times and cancellations may have changed since.',
  'offline.notStored': 'This plan is not stored on this device.',
  'offline.notStored.detail': 'Open it once while connected and it will be here next time.',

  // Errors
  'error.generic': 'Something went wrong.',
  'error.offline': 'You appear to be offline.',
  'error.notFound': 'We could not find that.',
} as const
