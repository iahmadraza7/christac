# Knowledge Restructure Spec — The Courtship Decoder

Input: REV 7_The Courtship Decoder.docx
Output: 5 markdown files uploaded to the GPT as knowledge

---

## HARD RULES

1. **Reorganize only. Never reword.** Every failing sign, passing sign, teaching note and coaching response is copied character for character from the source. No smoothing, no merging near duplicates, no fixing her grammar, no shortening.
2. **Delete the behavior rules.** Roughly the first third of the source is persona, voice, banned phrases, conversation flow and global rules. All of that now lives in the instructions field. It must not appear in any knowledge file, or the old conflicts come straight back.
3. **Nothing is invented.** If a filter is missing from the source, leave it missing and flag it. Do not write coaching that is not there.
4. **Count and verify.** Number of filters in equals number of filters out. Number of coaching responses in equals number out.

---

## FILE SPLIT

| File | Covers |
|---|---|
| `01-stage-1-before-first-date.md` | Before the first date |
| `02-stage-2-phase-1.md` | Dates 1 and 2 |
| `03-stage-2-phase-2.md` | Date 3 until he asks for exclusivity |
| `04-stage-3-standard-to-proposal.md` | After the No Girlfriend Standard conversation until he proposes |
| `05-stage-4-engagement-to-altar.md` | After the proposal until the wedding |

Stage 2 is split by phase deliberately. Phase confusion is one of the reported bugs, and separate files make it structurally harder for the model to pull Phase 2 content for a Phase 1 woman.

---

## BLOCK TEMPLATE

Retrieval returns chunks, not files. A chunk that reads only "Response if he Fails" carries no clue about which stage or filter it belongs to, so the model blends it with another filter. Every block therefore repeats its own stage and filter name inside itself. This repetition is deliberate and must not be tidied away.

Each filter becomes exactly this shape:

```
## STAGE <n> <PHASE if applicable> — FILTER: <filter name>

APPLIES TO: Stage <n><, Phase n>. <one line describing when she is in this stage>

### Automatic Failing Signs — Stage <n> <phase>, <filter name>
<her list, verbatim>

### Teaching Notes for Failing Signs — Stage <n> <phase>, <filter name>
<her notes, verbatim>

### DELIVER AS WRITTEN — Fail response, Stage <n> <phase>, <filter name>
<her coaching response, verbatim>

### Passing Signs — Stage <n> <phase>, <filter name>
<her list, verbatim>

### Teaching Notes for Passing Signs — Stage <n> <phase>, <filter name>
<her notes, verbatim>

### DELIVER AS WRITTEN — Pass response, Stage <n> <phase>, <filter name>
<her coaching response, verbatim>
```

Stage 3 and Stage 4 also carry Proceed With Caution and Warning Signs sections. Those get the same treatment, with their own `DELIVER AS WRITTEN` response block and the same stage and filter name repeated in every heading.

The literal string `DELIVER AS WRITTEN` is what the instructions field keys on. It must appear on every coaching response heading and nowhere else.

---

## FLAG, DO NOT FIX

Report these back rather than solving them:

- Stage 2 Phase 1 previously jumped from Filter 3 to Filter 6. Check whether REV 7 added 4 and 5.
- Stage 2 Phase 2 previously had two near duplicate filters on increasing investment and two on leadership toward exclusivity. If they are still duplicated, list them. She decides whether to merge, not us.
- Any filter with failing signs but no written coaching response.
- Any coaching response that names the framework, the decoder, the guidance, the methodology or the filters. Those break the instructions and she needs to reword them herself.

---

## CURSOR PROMPT

> Convert the attached document into the five knowledge files described in the spec. You are restructuring, not editing. Copy every failing sign, passing sign, teaching note and coaching response character for character from the source. Do not reword, do not merge similar items, do not correct grammar or spelling, do not shorten anything. Strip out the persona, voice, banned phrase and conversation flow sections entirely. Apply the block template to every filter, repeating the stage and filter name inside each heading exactly as shown. When you are done, output a count of filters and coaching responses per file, plus the flag list.
