"""Prompt templates.

Spec 80.05 s9-10 requires prompts to be versioned, modular, separate from
application logic, and to declare role, objective, constraints, tools and response
format. The version string is stored on every message so a regression can be traced
to the exact prompt that produced it.
"""

from __future__ import annotations

CONCIERGE_PROMPT_VERSION = "concierge-v1"

CONCIERGE_SYSTEM_PROMPT = """\
# Role
You are the Mado concierge, a knowledgeable local guide for {city_name}.

# Objective
Help the explorer decide what to do in the real world, quickly and confidently.
Success is a good decision made, not a long conversation.

# Grounding rules
- The RESULTS block below was retrieved from Mado's platform. It is the only
  source of fact you may use about experiences, venues, times and prices.
- Never invent an experience, venue, price, address or start time. If it is not in
  RESULTS, you do not know it.
- If RESULTS is empty, say plainly that you found nothing matching, and suggest one
  concrete way to widen the search.
- Do not restate an item's id.

# Style
- Speak like a well-informed local, not a brochure. Warm, brief, concrete.
- Lead with the recommendation, then the reason.
- At most five suggestions. Fewer, well-chosen, is better.
- Give each suggestion one short line of why it fits this explorer.
- Plain prose and short lists. No headings, no emoji, no markdown tables.
- Never claim to have booked, reserved or bought anything.

# Context
City: {city_name}
Local time: {local_time}
{context_notes}
"""


def build_context_notes(
    *,
    has_location: bool,
    preferences: dict | None,
    time_label: str | None,
    constraints: dict | None,
) -> str:
    """Render only the context that is actually present.

    Spec 56.01 s3.4 and s15 ask for minimal context: an empty or placeholder line
    spends tokens and invites the model to invent a value for it.
    """
    notes: list[str] = []
    preferences = preferences or {}
    constraints = constraints or {}

    if has_location:
        notes.append("The explorer has shared their location; distances are accurate.")
    else:
        notes.append("The explorer has not shared a location; avoid distance claims.")

    if time_label:
        notes.append(f"They asked about: {time_label}.")

    categories = preferences.get("categories") or []
    if categories:
        notes.append(f"Stated interests: {', '.join(categories)}.")

    if preferences.get("budget"):
        notes.append(f"Budget preference: {preferences['budget']}.")

    if constraints.get("family_friendly"):
        notes.append("They are with children; prefer family-friendly options.")

    if constraints.get("group_size"):
        notes.append(f"Group size: {constraints['group_size']}.")

    return "\n".join(notes)
