# Lessons Layer Spec — High League Positioning

Input: `FRAMEWORK_LESSONS.docx` (the Aug 30 version, ~37,600 words)
Output: 5 markdown files, one per stage, matching the decode knowledge files

---

## WHAT THIS LAYER IS FOR

The decode files answer "what is he showing". These answer "so what does she do about it, and how does she hold it".

They are reached only after a verdict exists. They never help decide one.

---

## HARD RULES

1. **Reorganize only. Never reword.** Her lessons are already in her voice. Copy them character for character.
2. **Nothing invented.** No connective coaching, no summaries, no headings that assert something she did not write.
3. **Every block marked as follow up.** The instructions gate on it.
4. **Report every removal.** She approves the strip list before it ships.

---

## FILE SPLIT

Five files, named to sit alongside the decode files:

| File | Stage |
|---|---|
| `L1-stage-1-lessons.md` | Stage 1 |
| `L2-stage-2-phase-1-lessons.md` | Stage 2 Phase 1 |
| `L3-stage-2-phase-2-lessons.md` | Stage 2 Phase 2 |
| `L4-stage-3-lessons.md` | Stage 3 |
| `L5-stage-4-lessons.md` | Stage 4 |

## CROSS STAGE CONTENT GETS DUPLICATED

The document has sections scoped to more than one stage: "STAGES 1 THRU 4", "Stages 1 thru 3", "ALL STAGES" (twice), "Stage 2 Phase 2 thru Stage 3".

Only one stage file is ever in play during a conversation, so shared content has to be copied into every stage file it applies to. Do not create a shared sixth file and do not link between files. Duplication across files is correct here and must not be optimized away.

Map each section by its own scope line. "Stages 1 thru 3" goes into L1, L2, L3 and L4, because Stage 3 in her numbering is the fourth file.

---

## BLOCK TEMPLATE

```
## FOLLOW UP — <stage label> — <lesson title>

USE: after a verdict only. Never to reach one.

<her slides, verbatim, in order>
```

The literal string `FOLLOW UP` appears on every lesson heading and nowhere in the decode files. That is what the instructions key on.

---

## OVERLAP TO STRIP

Some slides restate the decode criteria in different words. Two versions of the same rule in play at once is what muddies matching, which is the problem this whole build exists to avoid.

Known overlaps, all in the Stage 1 material:

- `SLIDE 3 — AUTOMATIC FAILS — SWIPE LEFT IMMEDIATELY`
- `SLIDE 4 — PASSING SIGNS — WORTH SWIPING RIGHT`
- `SLIDE 3 — AUTOMATIC FAILS`
- `SLIDE 4 — PASSING SIGNS AND THE GREEN FLAGS WOMEN MISS`

Strip the enumerated criteria lists only. Keep the prose teaching around them, including anything in the green flags slide that is not already a filter passing sign, because that is genuine coaching rather than a duplicated rule.

Write every removal to `STRIP-REPORT.md`: which file, which slide, the exact lines removed, and which decode filter already covers it. She reviews that before anything ships. If a removal is not clearly a duplicate, leave it in and list it as a question instead.

---

## FLAG, DO NOT FIX

- Any lesson that softens a position the filters take a hard line on. That contradiction produces hedging, which is the exact failure she reported. List them, do not resolve them.
- Any lesson that names a framework, methodology, filter or the Decoder. Those break the instructions when read aloud.
- Any section whose stage scope is ambiguous from its own heading.

---

## VERIFY BEFORE HANDING BACK

- Every content line from the source appears in at least one output file, except the lines in `STRIP-REPORT.md`.
- No output file contains a decode criteria list.
- Word count in equals word count out, plus duplication, minus the strip report.
