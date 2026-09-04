"""
Checks for tools/read-notes.py, against a .docx built here carrying real Word
comments - the actual file format, not a mock.

Run:  python tools/test_read_notes.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("read_notes", HERE / "read-notes.py")
rn = importlib.util.module_from_spec(_spec)
# Register before executing: @dataclass resolves its own module through
# sys.modules, and a module loaded by path is not there yet.
sys.modules["read_notes"] = rn
_spec.loader.exec_module(rn)

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -> {detail}" if detail else ""))


tmp = pathlib.Path(tempfile.mkdtemp())
fixture = tmp / "notes.docx"
subprocess.run([sys.executable, str(HERE / "make_test_docx.py"), str(fixture)],
               check=True, capture_output=True)

doc = rn.load(str(fixture))
by_id = {c.id: c for c in doc.comments}

print("\nEvery comment is found, with the text it is anchored to")
check("all six comments read", len(doc.comments) == 6, str(len(doc.comments)))
check("a plain selection keeps its anchor",
      by_id["0"].anchored_text == "first pass", repr(by_id["0"].anchored_text))
check("the comment text comes through",
      by_id["0"].text == "Use the softer wording here, only for early signals.")
check("the author is read", by_id["0"].author == "Christa Collins")
check("the date is read", by_id["0"].date.startswith("2026-09-03"))

print("\nThe awkward shapes")
check("a range spanning two paragraphs is joined",
      by_id["2"].anchored_text ==
      "This sentence begins a range that keeps going\nacross a second paragraph "
      "before it closes.", repr(by_id["2"].anchored_text))
check("a note of two paragraphs keeps both",
      by_id["2"].text.count("\n") == 1, repr(by_id["2"].text))
check("two comments on one line keep separate anchors",
      by_id["3"].anchored_text == "here" and by_id["4"].anchored_text == "there",
      f'{by_id["3"].anchored_text!r} {by_id["4"].anchored_text!r}')
check("a comment dropped on a point is reported as such",
      by_id["5"].had_range is False and by_id["5"].paragraph == 7,
      f"had_range={by_id['5'].had_range} para={by_id['5'].paragraph}")

print("\nThe body is still readable, and comments sit beside their text")
check("the body is intact", len(doc.paragraphs) == 8, str(len(doc.paragraphs)))
check("the first line is the first line",
      doc.paragraphs[0] == "Notes for developer, September 3.")
check("each comment knows its paragraph",
      [by_id[i].paragraph for i in "01234"] == [2, 3, 5, 6, 6],
      str([by_id[i].paragraph for i in "01234"]))

out = subprocess.run([sys.executable, str(HERE / "read-notes.py"), str(fixture)],
                     capture_output=True, text=True, encoding="utf-8")
check("it runs and reports the count", "6 comments" in out.stdout, out.stderr[-200:])
check("a comment is printed next to its own line",
      out.stdout.index("first pass") < out.stdout.index("only for early signals"))

print("\nFiles that are not what they claim")
empty = subprocess.run([sys.executable, str(HERE / "read-notes.py"),
                        "FRAMEWORK_LESSONS.docx"], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(HERE.parent))
check("a docx with no comments says so plainly",
      "0 comments" in empty.stdout and empty.returncode == 0)
bad = subprocess.run([sys.executable, str(HERE / "read-notes.py"),
                      str(HERE / "read-notes.py")], capture_output=True, text=True,
                     encoding="utf-8")
check("a non-docx gets a sentence, not a traceback",
      "is not a .docx" in (bad.stdout + bad.stderr)
      and "Traceback" not in (bad.stdout + bad.stderr))
missing = subprocess.run([sys.executable, str(HERE / "read-notes.py"), "nope.docx"],
                         capture_output=True, text=True, encoding="utf-8")
check("a missing file gets a sentence too",
      "No such file" in (missing.stdout + missing.stderr)
      and "Traceback" not in (missing.stdout + missing.stderr))

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
