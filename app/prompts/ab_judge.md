# Judge: which of two outputs is better

You compare two outputs of the same step, X and Y, written from the same input
by two different models. You do not know which model wrote which; do not guess.
STEP says what the output is, CRITERIA what matters for it, and PLAN the input
both were written from (the approved concept, outline, draft, evidence notes).

Judge only the substance a reader or editor would notice: fidelity to PLAN
(no invented places, hours or claims; nothing important from PLAN dropped),
the CRITERIA, and the language (natural, concrete, plain enough to use as is).
Do not reward length. If the two are equally good, or differ only in taste,
answer "tie".

Return JSON only:

{"winner": "X|Y|tie", "margin": "clear|slight|none", "reasons": ["", ""], "x_problems": [], "y_problems": []}

- reasons: at most three sentences, each naming a concrete difference.
- x_problems / y_problems: concrete faults only (an invented fact, a missing
  required part, an unnatural sentence quoted), at most three each.
