/**
 * Amharic messages (አማርኛ).
 *
 * Amharic is the working language of Addis Ababa, which is Mado's pilot city,
 * so this is not a courtesy translation - it is the language a large share of
 * the people this is built for would rather read.
 *
 * Three things that shaped the wording:
 *
 * - **Word order.** Amharic is subject-object-verb. "Starts at 8" is "በ8
 *   ይጀምራል" - literally "at-8 it-starts". Any message built by appending an
 *   English-shaped clause would land the verb in the middle, so every entry
 *   here is a complete sentence with the placeholders where the grammar wants
 *   them, not where English put them.
 * - **Zero is singular.** CLDR puts 0 in the `one` category for Amharic, so
 *   `.one` covers both "no places" and "one place" and reads correctly for
 *   both. That is why plural selection goes through `Intl.PluralRules` rather
 *   than a comparison with 1.
 * - **Loanwords are left alone where they are what people say.** Nobody in
 *   Addis asks for a "ቡና" using a coined technical term for "search"; ፈልግ is
 *   the ordinary word and is what appears here.
 *
 * Anything missing falls back to English rather than showing a key, and
 * `untranslated('am')` is asserted in the tests so the gap is a number somebody
 * can watch rather than something an explorer discovers.
 */

export const am = {
  // Navigation
  'nav.discover': 'አስስ',
  'nav.search': 'ፈልግ',
  'nav.plan': 'እቅድ',
  'nav.saved': 'የተቀመጡ',
  'nav.lists': 'ዝርዝሮች',
  'nav.posts': 'ልጥፎች',
  'nav.moderation': 'ክትትል',
  'nav.orders': 'ትኬቶቼ',
  'nav.settings': 'ግላዊነት እና መረጃ',
  'nav.notifications': 'ማሳወቂያዎች',

  // Account
  'account.signIn': 'ግባ',
  'account.signOut': 'ውጣ',

  // Common actions
  'action.save': 'አስቀምጥ',
  'action.cancel': 'ሰርዝ',
  'action.retry': 'እንደገና ሞክር',
  'action.close': 'ዝጋ',
  'action.seeAll': 'ሁሉንም አሳይ',

  // Discovery
  'discover.title': 'ከተማዎን ያስሱ',
  'discover.loading': 'ለእርስዎ ነገሮችን በመፈለግ ላይ…',
  'discover.empty.title': 'እስካሁን ምንም የለም',
  'discover.empty.body': 'ሌላ ከተማ ይሞክሩ፣ ወይም ትንሽ ቆይተው ይመለሱ።',
  'discover.openNow': 'አሁን ክፍት',
  'discover.startingSoon': 'በቅርቡ ይጀምራል',
  'discover.free': 'ነጻ',

  // Search
  'search.placeholder': 'ቡና፣ ቀጥታ ሙዚቃ፣ ጸጥ ያለ ቦታ…',
  'search.submit': 'ፈልግ',
  'search.empty.title': 'ምንም አልተገኘም',
  'search.empty.body': 'ጥቂት ቃላት ይሞክሩ፣ ወይም የከተማዋን ሌላ ክፍል።',
  // Zero and one share this form in Amharic - see the note above.
  'search.results.one': '{count} ውጤት',
  'search.results.other': '{count} ውጤቶች',

  // Saving
  'saved.add': 'አስቀምጥ',
  'saved.remove': 'ተቀምጧል',
  'saved.signInFirst': 'ነገሮችን ለማስቀመጥ ይግቡ።',

  // Reservations
  'reserve.hold': 'ቦታ ያዝ',
  'reserve.holding': 'በመያዝ ላይ…',
  'reserve.cancel': 'ሰርዝ',
  'reserve.nothingToPay': 'አሁን የሚከፈል ነገር የለም - ይህ ቦታዎን ብቻ ይይዛል።',
  'reserve.remaining.one': '{count} ቦታ ብቻ ቀርቷል',
  'reserve.remaining.other': '{count} ቦታዎች ብቻ ቀርተዋል',
  'reserve.full': 'ሙሉ ተይዟል',
  'reserve.cancelled': 'ተሰርዟል',

  // Planning
  'plan.stops.one': '{count} መዳረሻ',
  'plan.stops.other': '{count} መዳረሻዎች',
  'plan.travel': '{minutes} ደቂቃ ጉዞ',
  'plan.startNavigating': 'መጓዝ ጀምር',
  'plan.stopNavigating': 'መጓዝ አቁም',
  'plan.positionStaysHere': 'ቦታዎ በዚህ መሣሪያ ላይ ይቆያል - ማዶ የት እንዳሉ አይነገረውም።',

  // Settings
  'settings.title': 'ግላዊነት እና መረጃ',
  'settings.language.title': 'ቋንቋ',
  'settings.language.subtitle':
    'ማዶ ለራሱ ቃላት የሚጠቀመው። ዝርዝሮች ጸሐፊያቸው እንደጻፋቸው ይቆያሉ።',
  'settings.language.saved': 'ተቀምጧል',

  // Voice and photo search (spec SRCH-004, SRCH-005)
  'search.voice.start': 'በመናገር ይፈልጉ',
  'search.voice.stop': 'ማዳመጥ አቁም',
  'search.voice.listening': 'በማዳመጥ ላይ… አሁን ይናገሩ።',
  'search.voice.denied': 'ማዶ ማይክራፎኑን መስማት አልቻለም። በአሳሽዎ ቅንብሮች ውስጥ ይፍቀዱ።',
  'search.voice.unsupportedLanguage':
    'በአማርኛ በመናገር መፈለግ እስካሁን የለም - አሳሾች አማርኛን ወደ ጽሑፍ መቀየር አይችሉም።',
  'search.visual.button': 'በፎቶ ይፈልጉ',
  'search.visual.tooBig': 'ያ ፎቶ በጣም ትልቅ ነው። ከ12 ሜባ በታች ይሞክሩ።',
  'search.visual.looksLike': 'የሚመስለው፦ {description}',
  'search.visual.notIdentification':
    'ይህ በፎቶው ውስጥ ያለውን ትክክለኛ ቦታ ሳይሆን እንደ ፎቶው ያሉ ቦታዎችን ያገኛል።',
  'search.visual.unclear': 'ያንን ፎቶ መለየት አልተቻለም።',
  'search.visual.unclear.detail': 'የቦታውን ግልጽ እና ቅርብ ፎቶ ይሞክሩ።',

  // Offline (spec EXP-005)
  'offline.banner': 'ማዶ ላይ መድረስ አልተቻለም። ያስቀመጧቸው እቅዶች አሁንም እዚህ አሉ።',
  'offline.savedCopy': 'ያስቀመጡት ቅጂ እየታየ ነው። ሰዓቶች እና ስረዛዎች ከዚያ በኋላ ተለውጠው ሊሆን ይችላል።',
  'offline.notStored': 'ይህ እቅድ በዚህ መሣሪያ ላይ አልተቀመጠም።',
  'offline.notStored.detail': 'ከመስመር ላይ ሆነው አንድ ጊዜ ይክፈቱት፣ በሚቀጥለው ጊዜ እዚህ ይሆናል።',

  // Errors
  'error.generic': 'የሆነ ችግር ተፈጥሯል።',
  'error.offline': 'ከመስመር ውጭ ያሉ ይመስላል።',
  'error.notFound': 'ያንን ማግኘት አልቻልንም።',
} as const
