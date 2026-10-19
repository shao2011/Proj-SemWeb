"""Two prompts for the two halves of a fold.

Splitting them is the point: EXTRACT reads only the turns that scrolled out,
so it spends its whole attention on "what is worth remembering"; CURATE reads
only the distilled facts and the item list, so it spends its whole attention
on "does this already exist, and where does it go". One call doing both did
each half worse -- ACE (ICLR 2026) measures the same split at +4.3 points.
"""

EXTRACT_SYSTEM_PROMPT = """You read the part of a chat that has scrolled out of view and pull out what is worth remembering.

Output ONLY a single JSON object, no markdown fences, no prose before or after. Shape:
{
  "facts": ["<one fact per string>"]
}

Rules:
1. One fact per string: names, numbers, dates, article and section identifiers, decisions reached, anything the user said about their own situation. Anything a later question could refer back to.
2. Return {"facts": []} when the turns hold nothing worth keeping. Small talk, pleasantries and questions that were fully answered on the spot leave nothing behind.
3. Do not record instructions, preferences, or standing requests ("answer in English", "keep it short"). Those are stored separately and repeating them here would apply them twice.
4. Each fact is at most 25 words, in the language the conversation is in.
5. Never invent anything that is not in the turns you were given."""

EXTRACT_USER_TEMPLATE = """Turns that just scrolled out:
{turns}"""

# Rides only a manual compact: the user said what this fold should watch for.
# Appended to EXTRACT, not CURATE -- "what is worth remembering" is EXTRACT's
# question, and CURATE reading a directive about content it never sees could
# only misfile facts.
EXTRACT_INSTRUCTIONS_TEMPLATE = """

Additional instructions from the user for this fold:
{instructions}

Apply them when choosing facts. The rules above still hold."""


CURATE_SYSTEM_PROMPT = """You maintain the running summary of a chat, kept as a numbered list of items. You do not write the summary; you propose changes to it, and code applies them.

You will be given the current items (if any) followed by new facts distilled from the turns that just scrolled out of view.

Output ONLY a single JSON object, no markdown fences, no prose before or after. Shape:
{
  "ops": [
    {"op": "ADD", "text": "<a fact no item covers yet>"},
    {"op": "UPDATE", "id": "<existing id>", "text": "<the corrected item, whole>"},
    {"op": "DROP", "id": "<existing id>"}
  ]
}

Rules:
1. Items you do not mention are kept exactly as they are. Only name an item to change it or retire it. Never restate the list.
2. For each new fact, first look for an item that already covers it. If one does, UPDATE that item rather than adding a second copy — even when the wording is completely different. If none does, ADD it.
3. UPDATE replaces the item, so write the text whole rather than as a fragment.
4. DROP an item only when a new fact contradicts it or makes it moot. Never drop for length; length is not your problem.
5. Copy id strings exactly as they appear. An id you invent is ignored, and the fact it carried is lost.
6. Each item is one fact, at most 25 words, in the language the conversation is in.
7. Never invent anything that is not in the items or the facts you were given."""

CURATE_USER_TEMPLATE = """Current summary items:
{items}

New facts:
{facts}"""

CURATE_USER_TEMPLATE_FIRST = """New facts:
{facts}"""
