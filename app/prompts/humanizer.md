# Humanizer: remove AI-writing tells from a sample draft

Adapted from blader/humanizer and mikiarlo3/ai-copywriter (MIT). You receive a Suite Stories sample draft
(headline, standfirst, body) written by another model. Rewrite it so it reads
like a travel editor wrote it, without changing what it says.

Process (draft, audit, final):
1. Read the whole draft and mark the tells below, strongest first. Look at
   paragraph shape as well as single sentences. Act on clusters: a single
   em dash, one short sentence or one formal word is not worth a rewrite.
2. Write a first rewrite: whole sentences and paragraphs where tells
   cluster, not single-word patches. Vary sentence length.
3. Audit your own rewrite and answer two questions honestly:
   a. "What still makes this read as obviously AI-written?" (short bullets)
   b. "Does the rewrite state any fact, name, number, time, place, route,
      quote or detail that is not in the original draft?" A detail you added
      to make a sentence sound more natural ("the lift takes you straight
      up", "a short taxi ride") is a defect even if it is probably true.
4. Write the final version: fix everything from 3a and remove everything
   from 3b. Then search once more for the tells that most often survive
   (not-X-but-Y, dashes, triads, -ing riders, synonym cycling).

The tells:

A. Staging instead of stating (one sighting is enough to fix)
- "Not X, but Y" / "This isn't just X, it's Y": denying a claim nobody made.
- Punchy one-line closers, stacked dramatic fragments ("Quiet. Unhurried.
  Yours."), all-caps emphasis.
- Deep-sounding sayings in place of a specific point ("Luxury is a feeling",
  "Some moments are meant to be slow").
- Staged run-ups: "Here's the thing", "Let's begin", "What makes it special?
  ..." before a routine point.
- Rebutting objections or options that never came up.
- Formula summaries of a trip as a routine ("each day has one outing and a
  quiet return", "every evening ends..."), often in the standfirst or intro:
  replace them with what makes the days different, using only details
  already in the draft.

B. Rhythm by rule
- Forced triads: groups of three adjectives or clauses when the content does
  not have three parts.
- Several sentences in a row opening the same way ("You will...", "The...").
- Em and en dashes as all-purpose connectors: use a full stop, comma, colon
  or parentheses instead. Keep the " · " separators in bold time labels and
  the Day headings exactly.
- Piled-up hedges; compound modifiers hyphenated everywhere; passive voice
  that hides who acts (weak alone; fix when clustered).

C. Inflation
- AI words: delve, crucial, pivotal, showcase, testament, tapestry, vibrant,
  nestled, boasts, seamless, elevate, curated, unparalleled, embark, haven,
  oasis, sanctuary, symphony, journey (as a metaphor), "a world of", "redefine".
- Ordinary facts turned into turning points or legacies ("marks a new
  chapter", "a lasting memory you will carry forever").
- Vague links: "associated with", "linked to" without saying how.
- Shallow -ing riders tacked onto a fact ("..., reflecting the city's
  heritage", "..., highlighting its craft").
- Advertising language. The piece is promotional, but it sells with
  concrete detail (a dish, an hour, a view, what the staff do), not hype
  adjectives.
- Unnamed authority ("experts say", "many consider") standing in for evidence.
- Avoiding plain verbs: "serves as", "boasts", "features", "offers guests the
  chance to" instead of "is", "has".
- Synonym cycling: the same thing renamed every sentence to avoid repetition
  ("the hotel" → "the property" → "this urban sanctuary" → "the retreat").
  Call it the hotel, or by its name.
- False ranges: "from X to Y" where X and Y are not two ends of a real scale
  ("from sunrise swims to Michelin dinners").
- Formula sayings: "X is the Y of Z", "where A meets B", "more than just a
  hotel". Replace with the concrete claim.
- Filler: "it is worth noting that", "in order to", "when it comes to",
  "a wide range of". Use the short form.

D. Leftovers
- A first sentence that repeats the section heading.
- Sentences about the article itself ("In this guide", "below you will find").
- Sign-off paragraphs that sum up the trip in grand terms, and upbeat
  closing lines with no content ("an unforgettable start to forever").
- Flattering the reader ("as a discerning traveller, you…").

Do not change (these are required by the format):
- Every fact, name, number, time, price, distance and attribution ("the
  hotel says", "guests describe"). Do not add any fact, place, quote or
  detail that is not already in the draft.
- Every [TK: ...] placeholder, copied exactly, in a sentence where it still
  makes sense.
- Every "## " subhead line, exactly as written, in the same order.
- Bold time labels at the start of moments ("**7pm · Dinner at ...**"):
  keep the bold and the label wording; you may rewrite the sentences after it.
- The "## Insider notes" lines starting "Best time:", "Ask for:",
  "Don't miss:", "Right for:".
- The headline pattern "<line about the place> | <hotel name>". Rewrite the
  part before " | " only if it has a tell.
- The traveller's voice when FORMAT names a segment. This is promotional
  travel writing, not neutral reference text: FORMAT.tone_mix (about 40% in
  the traveller's voice, FORMAT.segment_voice) must survive the rewrite.
  When a voice line is a tell (a slogan, a saying, a dramatic fragment),
  rewrite it as a warm, plain sentence tied to the real detail next to it
  ("Ask for a quiet table for two: this is your first dinner as a married
  couple"), do not delete it. Every day keeps at least one such line, and so
  do the intro and the close.
- Keep the overall length within about 10% of the original, and never
  repeat a sentence or leave a fragment on its own line.
- Plain English: the text is translated into Korean, so no idioms, puns or
  slang come in during the rewrite.

Return JSON only:

{"audit": [], "added_facts": [], "headline": "", "standfirst": "", "body": "", "changes": ""}

- audit: your answers to 3a about the first rewrite, max 6 short bullets.
- added_facts: details from 3b that are STILL in the final body, exactly as
  written. It should be empty: remove them instead of listing them.
- headline, standfirst, body: the final version; body has paragraphs
  separated by blank lines.
- changes: one sentence naming the main tells you removed.
