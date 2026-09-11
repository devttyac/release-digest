#!/usr/bin/env python3
"""Render the fetched/ranked digest JSON into email-safe HTML.

Usage:
    .venv/bin/python render_digest.py [JSON_PATH] [--out PATH]

Design constraints here are the product of two failed real test sends, not
taste. Read before changing anything:

1. NO <style> BLOCK. Proton Mail strips it entirely, which silently collapses
   the whole design to unstyled HTML. Every style is inlined per element.

2. LIGHT THEME, NOT DARK. The second attempt used a dark violet ground with
   near-white text. Proton stripped the background but kept the text colors —
   white-on-white, effectively invisible. A dark email cannot degrade
   gracefully: if the background is dropped, light text is unreadable. Light
   ground + dark text stays readable no matter what the sanitizer removes.
   Background colors are also set via the `bgcolor` attribute, which survives
   sanitization better than CSS, as a second layer.

3. NO CSS GRID / FLEXBOX. Multi-column layout uses <table>.

4. SMALL SIDE THUMBNAILS, NOT BANNER IMAGES. Most clients block remote images
   until the reader opts in. A full-width banner leaves a large empty hole in
   that state; a small side thumbnail leaves a minor gap and the card still
   reads as a complete card.
"""

import argparse
import html
import json
import sys
from pathlib import Path

PAGE_BG = "#f6f5fa"
CARD_BG = "#ffffff"
PICK_BG = "#f3f0ff"
BORDER = "#e4e0ee"
ACCENT = "#6d4aff"
INK = "#17141f"
INK_DIM = "#6a6480"
INK_FAINT = "#6f6a85"  # kept dark enough to clear ~4.5:1 on white, since a stripped background falls back to white
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


BLURB_CHARS = 105  # keeps most cards to 2-3 lines, so heights across a row stay close


def clamp(text: str, limit: int = BLURB_CHARS) -> str:
    """Clamp a stored overview to display length at a word boundary.

    Wildly uneven blurb lengths were the main source of ragged, untidy rows —
    one card running six lines next to one running none. Cards still equalize
    via height:100%, but keeping the text itself in a narrow band means the
    equalized height is close to the natural height for every card.
    """
    text = (text or "").rstrip("…").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1][:3]} {int(d)}"


def thumb_size(category: str) -> tuple[int, int]:
    # RAWG game art is landscape; TMDB posters are portrait. Cropping either
    # into the other's box wastes most of the image.
    return (96, 64) if category == "games" else (64, 96)


def card_html(item: dict, category: str, cid_map: dict[str, str] | None = None) -> str:
    pick = item.get("top_pick")
    border = ACCENT if pick else BORDER
    bg = PICK_BG if pick else CARD_BG
    title = esc(item["title"])
    date = fmt_date(item["release_date"])

    meta_extra = ""
    if category == "games" and item.get("platforms"):
        meta_extra = " · " + esc(", ".join(item["platforms"][:3]))
    elif category == "movies":
        meta_extra = " · Theatrical"

    pill = (
        f'<span style="display:inline-block;background:{ACCENT};color:#ffffff;font-size:10px;'
        f'font-weight:700;text-transform:uppercase;letter-spacing:0.05em;padding:2px 7px;'
        f'border-radius:999px;font-family:{FONT};margin-bottom:5px;">Top Pick</span><br>'
        if pick else ""
    )

    blurb = ""
    if item.get("overview"):
        blurb = (
            f'<div style="font-size:12px;color:{INK_DIM};line-height:1.45;'
            f'font-family:{FONT};margin-top:5px;">{esc(clamp(item["overview"]))}</div>'
        )

    # Link through to the title's page on TMDB / RAWG. Colour and
    # text-decoration are set explicitly because clients restyle bare links —
    # left alone, these turn blue-and-underlined and the cards get noisy.
    url = item.get("url")
    linked_title = (
        f'<a href="{esc(url)}" style="color:{INK};text-decoration:none;">{title}</a>'
        if url else title
    )

    text_cell = f"""<td valign="top" style="padding:0;">
      {pill}<div style="font-weight:600;font-size:15px;line-height:1.25;color:{INK};font-family:{FONT};">{linked_title}</div>
      <div style="font-size:12px;color:{INK_FAINT};font-family:{FONT};margin-top:3px;">
        <span style="color:{ACCENT};font-weight:600;">{date}</span>{meta_extra}
      </div>
      {blurb}
    </td>"""

    image = item.get("image")
    if image:
        # When the image has been embedded as an inline attachment, reference it by
        # Content-ID instead of its remote URL — clients block remote fetches by
        # default, but never block parts carried inside the message itself.
        src = f"cid:{cid_map[image]}" if cid_map and image in cid_map else image
        w, h = thumb_size(category)
        img = (
            f'<img src="{esc(src)}" alt="{title}" width="{w}" height="{h}" '
            f'style="width:{w}px;height:{h}px;object-fit:cover;display:block;border-radius:6px;'
            f'border:1px solid {BORDER};">'
        )
        if url:
            # border:0 matters — some clients draw a link border around a
            # wrapped image otherwise.
            img = f'<a href="{esc(url)}" style="text-decoration:none;border:0;">{img}</a>'
        thumb_cell = (
            f'<td valign="top" width="{w + 12}" style="padding:0 12px 0 0;">{img}</td>'
        )
    else:
        thumb_cell = ""

    # The card IS the table cell — its background, border and radius live on the
    # <td> itself rather than on a nested table. Cells in a table row are always
    # equal height, so two cards side by side match automatically. The previous
    # nested-table version only ever took its content height, which left a short
    # card floating above a gap next to a tall one, and `height:100%` on a table
    # inside an auto-height cell is not honoured consistently across clients.
    return f"""<td width="50%" valign="top" bgcolor="{bg}"
  style="background:{bg};border:1px solid {border};border-radius:12px;padding:14px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>{thumb_cell}{text_cell}</tr>
  </table>
</td>"""


def category_table(items: list[dict], category: str, cid_map: dict[str, str] | None = None) -> str:
    if not items:
        return f'<p style="color:{INK_FAINT};font-family:{FONT};font-size:13px;">No releases found this month.</p>'
    rows = []
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        cells = "".join(card_html(it, category, cid_map) for it in pair)
        if len(pair) == 1:
            # Keep the lone card at half width rather than letting it stretch.
            cells += '<td width="50%" style="border:0;"></td>'
        rows.append(f"<tr>{cells}</tr>")
    # cellspacing supplies the gutters between cards. Padding on the card cells
    # can't do it — that sits inside the border, so the cards would touch.
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="12" border="0" '
        'style="border-collapse:separate;border-spacing:12px;">'
        + "".join(rows) + "</table>"
    )


def section_html(title: str, items: list[dict], category: str, cid_map: dict[str, str] | None = None) -> str:
    return f"""
<tr><td style="padding:0 0 10px;">
  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;
    color:{ACCENT};font-family:{FONT};margin-bottom:10px;">{esc(title)}</div>
  {category_table(items, category, cid_map)}
</td></tr>
<tr><td style="height:18px;line-height:18px;font-size:0;">&nbsp;</td></tr>
"""


def render(data: dict, cid_map: dict[str, str] | None = None) -> str:
    year, month = data["month"].split("-")
    month_label = f"{MONTHS[int(month) - 1]} {year}"

    error_note = ""
    if data.get("errors"):
        error_note = (
            f'<tr><td style="padding:0 0 14px;"><div style="font-size:12px;color:{INK_FAINT};'
            f'font-family:{FONT};">Note: {esc("; ".join(data["errors"]))}</div></td></tr>'
        )

    body = "".join([
        section_html("Games", data.get("games", []), "games", cid_map),
        section_html("Movies", data.get("movies", []), "movies", cid_map),
        section_html("TV", data.get("tv", []), "tv", cid_map),
    ])

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body bgcolor="{PAGE_BG}" style="margin:0;background:{PAGE_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAGE_BG}" style="background:{PAGE_BG};">
<tr><td align="center" style="padding:0;">
<table role="presentation" width="660" cellpadding="0" cellspacing="0" border="0" style="max-width:660px;width:100%;">
<tr><td style="padding:28px 20px 0;">
  <div style="font-weight:700;font-size:17px;color:{ACCENT};font-family:{FONT};margin-bottom:20px;">release digest</div>
  <div style="border-bottom:2px solid {BORDER};padding-bottom:16px;margin-bottom:20px;">
    <div style="font-size:25px;font-weight:700;color:{INK};font-family:{FONT};margin-bottom:6px;">What's Dropping — {esc(month_label)}</div>
    <div style="font-size:14px;color:{INK_DIM};font-family:{FONT};line-height:1.5;">Games, movies, and TV releases this month, sorted latest-first by date. Highest-popularity title in each category marked Top Pick.</div>
  </div>
</td></tr>
{error_note}
<tr><td style="padding:0 20px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  {body}
  </table>
</td></tr>
<tr><td style="padding:0 20px 28px;">
  <div style="border-top:1px solid {BORDER};padding-top:14px;font-size:11px;color:{INK_FAINT};font-family:{FONT};line-height:1.6;">
    Sent automatically by release-digest.
  </div>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", nargs="?", default="/tmp/release-digest-data.json")
    parser.add_argument("--out", default="/tmp/release-digest-email.html")
    args = parser.parse_args()

    data = json.loads(Path(args.json_path).read_text())
    Path(args.out).write_text(render(data))
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
