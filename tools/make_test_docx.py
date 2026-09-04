"""
Build a .docx carrying real Word comments, so read-notes.py is tested against
the actual file format rather than a mock. python-docx cannot write comments,
so the parts are assembled by hand - which is the same shape Word produces.

Covers the cases that matter: a plain selection, a range spanning two
paragraphs, two comments on the same paragraph, and one dropped on a point
with no selection at all.
"""
import pathlib
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"')

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>"""


def run(text):
    return f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"


def start(i):
    return f'<w:commentRangeStart w:id="{i}"/>'


def end(i):
    return (f'<w:commentRangeEnd w:id="{i}"/>'
            f'<w:r><w:commentReference w:id="{i}"/></w:r>')


BODY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {NS}><w:body>
<w:p>{run("Notes for developer, September 3.")}</w:p>
<w:p>{run("The verdict should be softer on the ")}{start(0)}{run("first pass")}{end(0)}{run(" when the signal is early.")}</w:p>
<w:p>{run("She said ")}{start(1)}{run("sure")}{end(1)}{run(" and it started a new man.")}</w:p>
<w:p>{start(2)}{run("This sentence begins a range that keeps going")}</w:p>
<w:p>{run("across a second paragraph before it closes.")}{end(2)}</w:p>
<w:p>{run("Two notes sit on this one line: ")}{start(3)}{run("here")}{end(3)}{run(" and ")}{start(4)}{run("there")}{end(4)}{run(".")}</w:p>
<w:p>{run("This paragraph has a point comment with no selection.")}<w:r><w:commentReference w:id="5"/></w:r></w:p>
<w:p>{run("Last line of the body.")}</w:p>
</w:body></w:document>"""

COMMENT_TEXT = {
    "0": ("Christa Collins", "2026-09-03T09:14:00Z",
          "Use the softer wording here, only for early signals."),
    "1": ("Christa Collins", "2026-09-03T09:16:00Z",
          "An affirmative must not start a new man."),
    "2": ("Christa Collins", "2026-09-03T09:20:00Z",
          "This whole passage needs the positioning rule applied.\nSecond paragraph of the same note."),
    "3": ("Christa Collins", "2026-09-03T09:25:00Z", "First of two on this line."),
    "4": ("Christa Collins", "2026-09-03T09:26:00Z", "Second of two on this line."),
    "5": ("Christa Collins", "2026-09-03T09:31:00Z",
          "Dropped on a point, no text selected."),
}

parts = []
for cid, (author, date, text) in COMMENT_TEXT.items():
    paras = "".join(f"<w:p>{run(line)}</w:p>" for line in text.split("\n"))
    parts.append(f'<w:comment w:id="{cid}" w:author="{author}" '
                 f'w:date="{date}" w:initials="CC">{paras}</w:comment>')
COMMENTS_XML = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<w:comments {NS}>{''.join(parts)}</w:comments>")

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "fixture.docx")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", CONTENT_TYPES)
    z.writestr("_rels/.rels", RELS)
    z.writestr("word/_rels/document.xml.rels", DOC_RELS)
    z.writestr("word/document.xml", BODY)
    z.writestr("word/comments.xml", COMMENTS_XML)
print(f"wrote {out} with {len(COMMENT_TEXT)} comments")
