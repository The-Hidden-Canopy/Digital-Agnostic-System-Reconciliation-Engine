"""Tiny synthetic PDF writer for fixtures (text pages, optional image-only page). Not a general PDF library."""
from __future__ import annotations


def make_pdf(pages: list) -> bytes:
    """Each page is a list of text lines; None makes a page with no text layer; bytes is a ready-made
    zlib-compressed content stream (used to build decompression-bomb fixtures)."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # pages placeholder (object 2)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        if isinstance(lines, bytes):
            c = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(lines) + lines + b"\nendstream")
            pg = add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R >>" % c)
            kids.append(pg)
            continue
        if lines is None:
            content = b"0.9 g 50 50 200 200 re f"
        else:
            parts = [b"BT /F1 12 Tf 14 TL 50 740 Td"]
            for ln in lines:
                esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
                parts.append(b"(" + esc + b") Tj T*")
            parts.append(b"ET")
            content = b"\n".join(parts)
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        pg = add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                 b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (c, font))
        kids.append(pg)
    objs[1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(kids), b" ".join(b"%d 0 R" % k for k in kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
