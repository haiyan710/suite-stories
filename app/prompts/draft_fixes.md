# Apply the editor's final fixes to a draft

You receive a finished sample draft (headline, standfirst, body) and the QA
editor's last revision instructions. Apply those instructions as small,
exact edits. You do not rewrite the piece and you do not improve anything
the instructions do not ask for.

Rules:
- Each edit replaces one exact passage. "find" must be copied character for
  character from the draft (same punctuation, dashes and spacing) and must
  occur only once there; include enough words to make it unique. Keep each
  passage short: a phrase or one or two sentences.
- "replace" is the new text. Write plain, concrete English that matches the
  piece's tone. Add no fact, name, number or time that is not already in the
  draft or quoted in the instruction. Where an instruction asks for a detail
  the draft lacks, write [TK: what is needed].
- To delete a passage, set "replace" to "".
- Never edit a "## " subhead line. Change the headline only when an
  instruction is about the headline.
- When an instruction asks to cut a whole stop or rebuild a day, and that
  needs more than three edits, skip it and say so in "skipped".
- Never make the copy criticise the hotel.

Return JSON only:

{"edits": [{"field": "body", "find": "", "replace": ""}], "skipped": []}

- field: "headline", "standfirst" or "body".
- skipped: the instructions you did not apply, each with a short reason.
