#!/usr/bin/env python3
"""
Read a .docx and print the body text together with every margin comment, each
one shown against the text it is anchored to.

Word keeps comments out of the document body. `python-docx` does not expose
them, so a script that reads a .docx the obvious way sees the body and nothing
else - which is how a whole round of her instructions went unread.

Where they actually live:

    word/document.xml   the body, plus the anchors:
                        <w:commentRangeStart w:id="3"/> ... text ...
                        <w:commentRangeEnd w:id="3"/><w:commentReference w:id="3"/>
    word/comments.xml   the comments themselves, by the same w:id

A comment with no range - dropped on a single point rather than a selection -
has only the reference. That is reported as anchored to its paragraph.

Usage:
    python tools/read-notes.py FILE.docx              body and comments together
    python tools/read-notes.py FILE.docx --only-notes just the comments
    python tools/read-notes.py FILE.docx --json       machine readable
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DOCUMENT = "word/document.xml"
COMMENTS = "word/comments.xml"


@dataclass
class Comment:
    id: str
    author: str = ""
    date: str = ""
    initials: str = ""
    text: str = ""
    anchored_text: str = ""      # what she highlighted
    paragraph: int | None = None  # 1-based, where the comment ends
    had_range: bool = False       # False when dropped on a point, not a selection


@dataclass
class Document:
    paragraphs: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    # paragraph index -> comment ids that close there, so they can be printed
    # right after the text they belong to
    by_paragraph: dict[int, list[str]] = field(default_factory=dict)


def _text_of(elem: ET.Element) -> str:
    """The visible text of one element, tabs and breaks included."""
    out = []
    for node in elem.iter():
        tag = node.tag
        if tag == W + "t" and node.text:
            out.append(node.text)
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def read_comments(zf: zipfile.ZipFile) -> dict[str, Comment]:
    try:
        raw = zf.read(COMMENTS)
    except KeyError:
        return {}
    root = ET.fromstring(raw)
    found = {}
    for node in root.iter(W + "comment"):
        cid = node.get(W + "id")
        if cid is None:
            continue
        paras = [_text_of(p) for p in node.iter(W + "p")]
        found[cid] = Comment(
            id=cid,
            author=node.get(W + "author", "") or "",
            date=(node.get(W + "date", "") or "")[:19].replace("T", " "),
            initials=node.get(W + "initials", "") or "",
            text="\n".join(x for x in paras if x.strip()).strip(),
        )
    return found


def read_document(zf: zipfile.ZipFile, comments: dict[str, Comment]) -> Document:
    """
    Walk the body in document order, collecting paragraphs and, for every open
    comment range, the text it covers. A range can run past the end of a
    paragraph, so the collectors live outside the paragraph loop.
    """
    doc = Document()
    para: list[str] = []
    open_ranges: dict[str, list[str]] = {}
    para_no = 0

    for event, elem in ET.iterparse(io.BytesIO(zf.read(DOCUMENT)),
                                    events=("start", "end")):
        tag = elem.tag

        if event == "start":
            if tag == W + "commentRangeStart":
                cid = elem.get(W + "id")
                if cid is not None:
                    open_ranges[cid] = []
            continue

        # end events below
        if tag == W + "t":
            if elem.text:
                para.append(elem.text)
                for buf in open_ranges.values():
                    buf.append(elem.text)
        elif tag == W + "tab":
            para.append("\t")
            for buf in open_ranges.values():
                buf.append("\t")
        elif tag in (W + "br", W + "cr"):
            para.append("\n")
            for buf in open_ranges.values():
                buf.append("\n")
        elif tag == W + "commentRangeEnd":
            cid = elem.get(W + "id")
            if cid in open_ranges:
                c = comments.get(cid)
                if c:
                    c.anchored_text = "".join(open_ranges[cid]).strip()
                    c.had_range = True
                del open_ranges[cid]
        elif tag == W + "commentReference":
            cid = elem.get(W + "id")
            c = comments.get(cid)
            if c is not None and c.paragraph is None:
                c.paragraph = para_no + 1
                doc.by_paragraph.setdefault(para_no + 1, []).append(cid)
        elif tag == W + "p":
            para_no += 1
            doc.paragraphs.append("".join(para))
            para = []
            # a range that outlives the paragraph keeps a break in its text
            for buf in open_ranges.values():
                buf.append("\n")

    # A comment whose reference never appeared still deserves to be seen.
    for c in comments.values():
        if c.paragraph is None:
            doc.by_paragraph.setdefault(0, []).append(c.id)
    doc.comments = sorted(comments.values(), key=lambda c: (c.paragraph or 0, int(c.id) if c.id.isdigit() else 0))
    return doc


def load(path: str) -> Document:
    try:
        zf = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise SystemExit(f"No such file: {path}")
    except zipfile.BadZipFile:
        raise SystemExit(
            f"{path} is not a .docx. A .docx is a zip of xml parts; a .doc, an "
            "rtf, or a file renamed to .docx will not open. Re-save it from "
            "Word as .docx.")
    with zf:
        if DOCUMENT not in zf.namelist():
            raise SystemExit(f"{path} does not look like a .docx (no {DOCUMENT})")
        return read_document(zf, read_comments(zf))


def render_comment(c: Comment, indent: str = "    ") -> list[str]:
    who = c.author or "unknown"
    when = f", {c.date}" if c.date else ""
    head = f"{indent}>> NOTE [{c.id}] {who}{when}"
    lines = [head, indent + "   " + "-" * (len(head) - len(indent) - 3)]
    if c.had_range and c.anchored_text:
        shown = " ".join(c.anchored_text.split())
        if len(shown) > 300:
            shown = shown[:300] + " ..."
        lines.append(f'{indent}   on: "{shown}"')
    else:
        lines.append(f"{indent}   on: (dropped on a point, not a selection)")
    for line in (c.text or "(empty)").split("\n"):
        lines.append(f"{indent}   {line}")
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx")
    ap.add_argument("--only-notes", action="store_true",
                    help="print the comments and their anchors, not the body")
    ap.add_argument("--json", action="store_true", help="machine readable")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    doc = load(args.docx)

    if args.json:
        print(json.dumps({
            "file": args.docx,
            "paragraphs": doc.paragraphs,
            "comments": [{
                "id": c.id, "author": c.author, "date": c.date,
                "text": c.text, "anchored_text": c.anchored_text,
                "paragraph": c.paragraph, "had_range": c.had_range,
            } for c in doc.comments],
        }, indent=1, ensure_ascii=False))
        return 0

    n = len(doc.comments)
    print("=" * 72)
    print(f"{args.docx}")
    print(f"{len(doc.paragraphs):,} paragraphs, {n} comment{'' if n == 1 else 's'}")
    print("=" * 72)
    print()

    if n == 0:
        print("No comments in this file. If you were expecting some, check you are")
        print("opening the version she commented on - comments do not survive a")
        print("'save as plain text' or a copy-paste into a new document.")
        print()

    if args.only_notes:
        for c in doc.comments:
            where = f"paragraph {c.paragraph}" if c.paragraph else "unanchored"
            print(f"[{c.id}] {where}")
            print("\n".join(render_comment(c, indent="  ")))
        return 0

    for i, text in enumerate(doc.paragraphs, start=1):
        if text.strip():
            print(text)
        for cid in doc.by_paragraph.get(i, []):
            c = next((x for x in doc.comments if x.id == cid), None)
            if c:
                print()
                print("\n".join(render_comment(c)))

    for cid in doc.by_paragraph.get(0, []):
        c = next((x for x in doc.comments if x.id == cid), None)
        if c:
            print()
            print("\n".join(render_comment(c)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Piped into head, less, or a closed window. Not an error worth a
        # traceback; Windows reports the same thing as OSError EINVAL below.
        raise SystemExit(0)
    except OSError as exc:
        if exc.errno in (22, 32):
            raise SystemExit(0)
        raise
