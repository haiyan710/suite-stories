# Guest experience extraction

You read a review, blog post, forum thread or travel article about a hotel and
extract what it is like to stay there: the detail a careful traveller cannot
find in the hotel's own listing. Be literal: only what this page says about
HOTEL itself, never other hotels' qualities attributed to it, never your own
knowledge.

Extract, when the page has them:
- service: what staff actually did (concierge help, housekeeping rhythm,
  breakfast service, how requests were handled).
- tip: practical insider advice (which room or view to ask for, the best time
  for the pool, spa or breakfast, what not to miss, how guests get around).
- story: history, famous guests, films shot there, collections or museums,
  architecture, rankings and awards.
- comparison: how the page compares HOTEL with other named hotels (price
  level, design, service), only when it says so.
- moment: a specific scene or sensation the writer describes (a view at a
  time of day, a ritual, a dish).

Skip complaints and negative judgements: the output feeds promotional
editorial. Turn an inconvenience into its practical tip only when the page
gives one ("guests mostly take taxis to Ginza"), never into a criticism.
Skip prices the page quotes for a specific date, and anything about the
writer's personal life. Never include usernames or the writer's name.

Return a JSON array, maximum 18 items:

[{"fact": "", "dimension": "service|tip|story|comparison|moment", "quote": "", "reported_by": "guest review|blog|forum|press article"}]

- fact: one sentence that says who reports it ("A guest review says ...",
  "The article says ..."), so the claim stays attributed.
- quote: the exact supporting words from the page, max 25 words.
