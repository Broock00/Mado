"""The few strings the server has to write in somebody else's language.

Notifications only. Everything else the API says travels as a code that the
client renders (see `app/core/language.py` for why), but a notification is
composed once, stored, and read back later - sometimes by a scheduled job with
no request and no reader attached. It has to be in the recipient's language at
the moment it is written, because there is nobody around to translate it
afterwards.

**Amharic is not a word-for-word English sentence.** The two entries below are
phrased the way somebody would actually say them rather than translated
mechanically, and the placeholders sit where the grammar puts them - Amharic is
subject-object-verb, so a template that hard-codes English word order produces
something that parses and reads wrong.

A missing translation falls back to English rather than showing a key. A
notification that says `reminder.event.title` has failed at being a
notification; one in the wrong language has at least still said the thing.
"""

from __future__ import annotations

from app.core.language import DEFAULT
from app.core.logging import get_logger

logger = get_logger("mado.messages")

CATALOGUE: dict[str, dict[str, str]] = {
    "en": {
        "reminder.event.title": "{title} is on tonight",
        "reminder.event.body": "Starts at {time}",
        "reminder.event.body_at_venue": "Starts at {time} at {venue}",
        "reminder.plan.title": "{title} starts soon",
        "reminder.plan.body": "Your first stop is {stop} at {time}",
    },
    "am": {
        # "ዛሬ ማታ ነው" - "is tonight". The subject comes first and the verb last,
        # so the placeholder cannot simply be swapped into the English shape.
        "reminder.event.title": "{title} ዛሬ ማታ ነው",
        "reminder.event.body": "በ{time} ይጀምራል",
        "reminder.event.body_at_venue": "በ{time} በ{venue} ይጀምራል",
        "reminder.plan.title": "{title} በቅርቡ ይጀምራል",
        "reminder.plan.body": "የመጀመሪያ መዳረሻዎ በ{time} {stop} ነው",
    },
}


def translate(key: str, language: str | None = None, /, **params: object) -> str:
    """Render one message in `language`, falling back to English.

    Formatting failures fall back too rather than raising: a template with a
    placeholder the caller did not supply is a bug worth logging, and it is not
    worth failing a reminder over. The English template is tried next because it
    is the one every other template was derived from.
    """
    language = language if language in CATALOGUE else DEFAULT
    template = CATALOGUE[language].get(key) or CATALOGUE[DEFAULT].get(key)
    if template is None:
        logger.warning("message_key_missing", key=key)
        return key

    try:
        return template.format(**params)
    except (KeyError, IndexError) as exc:
        logger.warning("message_format_failed", key=key, language=language, error=str(exc))
        fallback = CATALOGUE[DEFAULT].get(key)
        if fallback is not None and language != DEFAULT:
            try:
                return fallback.format(**params)
            except (KeyError, IndexError):
                pass
        return key
