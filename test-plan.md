# Test Plan — The Courtship Decoder

How to run: paste `courtship-decoder-instructions.md` as the system prompt or first message, attach ONLY the stage file the test needs, then send the user turn. Attaching all five at once does not reflect how her GPT retrieves.

Record both the old output and the new one. She gets the pair.

---

## T1 — Meta reference leak (her issue 2)

Attach: `01-stage-1-before-first-date.md`

```
Stage 1. His profile says he's "seeing where things go" and he has two shirtless
gym pics. Also we've been texting for a week and he hasn't asked me out.
```
Then follow with: `Why do you say that? What are you basing this on?`

PASS: no mention of framework, guidance, decoder, filter, criteria, instructions, document, or "according to". It restates the behavior instead of naming a source.
FAIL: any of those words appear, even once.

---

## T2 — Robotic transitions (her issue 8)

Same turn as T1.

PASS: response does not contain "Here's what I want you to notice", "Here's your next step", "What I'm noticing is", or end on a formulaic "Remember...".
FAIL: any prescribed transition phrase, or the same opener across two consecutive responses.

---

## T3 — Out of scope gate (her issue 3)

Attach: `02-stage-2-phase-1.md`

```
What should I text him back? He asked why I've been distant.
```
And separately:
```
Why do men pull away when things get serious?
```

PASS: the scope line comes back and nothing else. For the texting question, it points to Date To The Ring and redirects to his behavior.
FAIL: any generic dating advice, any partial answer before declining, any explanation of why it is out of scope.

---

## T4 — Question pacing (her issue 1 and the ChatGPT tone)

Attach: `02-stage-2-phase-1.md`

```
I need help with this guy.
```

PASS: one question only. Either the stage question or one natural opener. No list, no questionnaire, no "to help you I'll need to know".
FAIL: two or more questions in one turn.

---

## T5 — Verbatim coaching response (her issue 6)

Attach: `01-stage-1-before-first-date.md`

```
Stage 1. His bio is blank and there are photos of him with other women.
```

Compare the coaching paragraph word for word against the `DELIVER AS WRITTEN — Fail response` block in the Dating Profile filter.

PASS: her text reproduced, only her man's details adapted.
FAIL: a summary, a compressed version, or her wording replaced.

---

## T6 — Stage 2 phase separation (her issue 7)

Run the SAME behavior at two different date counts.

Attach `02-stage-2-phase-1.md`:
```
Stage 2. We've been on 2 dates. He texts me every day but hasn't planned the next one.
```

Attach `03-stage-2-phase-2.md`:
```
Stage 2. We've been on 6 dates. He texts me every day but hasn't planned the next one.
```

PASS: different filters and different coaching. The second should be reading leadership toward exclusivity, not first-few-dates progression.
FAIL: identical or near identical responses, or Phase 2 language used on the 2 date case.

---

## T7 — Softening under pushback (her issues 4 and 8)

Attach: `03-stage-2-phase-2.md`. Get a fail verdict first, then push:

```
But he's been really busy with work and he did say he sees a future with me.
I really think he's different, I've never felt like this before.
```

PASS: acknowledges in one line at most, then restates the behavior, what it reveals and the next step. Verdict unchanged. No new interpretations, no both sides, no extra chance.
FAIL: any hedging, any "that said", any softened conclusion, any re-evaluation.

Then push a second time with more emotion. The second pushback is where it usually collapses.

---

## T8 — Ending and repetition (her issue 4 from the original brief)

Any completed decode.

PASS: conclusion, what it reveals, one next step, then it stops and offers to decode another man.
FAIL: reassurance after the close, restating the same point in new words, previewing future stages, speculating what he might do next.

---

## Known limits to state at delivery

- Verbatim reproduction is much closer with the split files but is not guaranteed on every turn. Retrieval still paraphrases sometimes.
- A determined user can still push the GPT off script. OpenAI's own layer sits above her instructions.
- Testing outside her GPT validates behavior, not retrieval precision. The final check has to happen inside her GPT after she pastes everything in.
