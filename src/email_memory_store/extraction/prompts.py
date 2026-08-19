from __future__ import annotations

EXTRACTION_PROMPT_HEADER = """\
You are an assistant that extracts structured information from email threads.

Analyze the following email thread and extract:
1. Action items: specific tasks assigned to people or that need to be done
2. Deadlines: specific dates or timeframes by which something must happen
3. Decisions: choices or conclusions that were made or agreed upon
4. A concise summary of the thread
5. Key points: the most important facts or outcomes

Be CONSERVATIVE. Only extract items that are clearly and explicitly stated.
Do NOT infer or guess. Only include items with confidence >= 0.5.

Return your response as a single strict JSON object with NO markdown fencing, NO code blocks, and NO text outside the JSON.

The JSON must have exactly these keys:
- "action_items": array of objects with keys: owner (string or null, use full name and email address if available e.g. "Example Person <person@example.test>", never just a first name alone), action_text (string), due_date (string "YYYY-MM-DD" or null), confidence (float 0.0-1.0)
- "deadlines": array of objects with keys: label (string), due_date (string "YYYY-MM-DD" or null), related_project (string or null), confidence (float 0.0-1.0)
- "decisions": array of objects with keys: title (string), decision_text (string), decided_by (array of strings), decided_at (string "YYYY-MM-DD" or null), confidence (float 0.0-1.0)
- "summary": string (one paragraph summary of the thread)
- "key_points": array of strings

If nothing is found for a category, return an empty array.
Only include action_items, deadlines, and decisions where confidence >= 0.5.

Example output format:
{"action_items": [], "deadlines": [], "decisions": [], "summary": "Thread summary here.", "key_points": []}

EMAIL THREAD:
"""


def render_extraction_prompt(thread_text: str) -> str:
    return EXTRACTION_PROMPT_HEADER + thread_text
