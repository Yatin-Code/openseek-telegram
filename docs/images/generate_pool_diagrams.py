"""Regenerate the PNG architecture illustrations embedded in README.md."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).parent
S = 2
BG = "#F3F7FA"
INK = "#132B40"
MUTED = "#526A7D"
BLUE = "#2876B8"
TEAL = "#168F83"
ORANGE = "#D77C2E"
PALE_BLUE = "#E5F1FA"
PALE_TEAL = "#E4F3F0"
PALE_ORANGE = "#FBF0E4"
LINE = "#C8D6E0"
WHITE = "#FFFFFF"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") if bold else
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",)
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size * S)
        except OSError:
            continue
    return ImageFont.load_default(size=size * S)


def canvas(width: int, height: int, eyebrow: str, title: str, subtitle: str):
    image = Image.new("RGB", (width * S, height * S), BG)
    d = ImageDraw.Draw(image)
    d.text((72 * S, 42 * S), eyebrow.upper(), font=font(13, True), fill=TEAL)
    d.text((72 * S, 70 * S), title, font=font(32, True), fill=INK)
    d.text((72 * S, 122 * S), subtitle, font=font(15), fill=MUTED)
    return image, d


def rounded(d: ImageDraw.ImageDraw, box, fill=WHITE, outline=LINE, radius=18, width=1):
    box = tuple(int(v * S) for v in box)
    d.rounded_rectangle(box, radius=radius * S, fill=fill, outline=outline, width=width * S)


def text(d: ImageDraw.ImageDraw, xy, value: str, size=15, fill=INK, bold=False):
    d.text((xy[0] * S, xy[1] * S), value, font=font(size, bold), fill=fill)


def line(d: ImageDraw.ImageDraw, points, color=LINE, width=3, arrow=True):
    scaled = [(int(x * S), int(y * S)) for x, y in points]
    d.line(scaled, fill=color, width=width * S, joint="curve")
    if arrow and len(scaled) >= 2:
        x1, y1 = scaled[-2]
        x2, y2 = scaled[-1]
        import math
        angle = math.atan2(y2 - y1, x2 - x1)
        size = 10 * S
        left = (x2 - size * math.cos(angle - 0.48), y2 - size * math.sin(angle - 0.48))
        right = (x2 - size * math.cos(angle + 0.48), y2 - size * math.sin(angle + 0.48))
        d.polygon([(x2, y2), left, right], fill=color)


def save(image: Image.Image, name: str):
    image.resize((image.width // S, image.height // S), Image.Resampling.LANCZOS).save(
        OUT / name, optimize=True)


def architecture():
    image, d = canvas(
        1600, 760, "OpenSeek architecture", "A community pool without a central OpenSeek API",
        "Every participant owns their node. Telegram holds accepted packages; each node keeps its own rebuildable index.")

    # Nuvio clients
    rounded(d, (64, 220, 420, 520), fill=WHITE, outline="#B8D5E7")
    rounded(d, (88, 242, 396, 282), fill=PALE_BLUE, outline=PALE_BLUE, radius=12)
    text(d, (108, 254), "NUVIO CLIENTS", 13, BLUE, True)
    text(d, (92, 312), "Generate thumbnails locally", 19, INK, True)
    text(d, (92, 354), "Exact file match first", 15, MUTED)
    text(d, (92, 382), "Hint match when exact misses", 15, MUTED)
    text(d, (92, 410), "Validate returned NVMT tiles", 15, MUTED)
    text(d, (92, 438), "Fill gaps with local frames", 15, MUTED)
    rounded(d, (92, 470, 392, 502), fill=PALE_ORANGE, outline=PALE_ORANGE, radius=10)
    text(d, (106, 478), "Sharing starts only after opt-in", 12, ORANGE, True)

    # Participant-owned nodes
    rounded(d, (580, 204, 1012, 386), fill=WHITE, outline="#B8D5E7")
    rounded(d, (604, 226, 988, 266), fill=PALE_BLUE, outline=PALE_BLUE, radius=12)
    text(d, (624, 238), "PARTICIPANT NODE A  ·  VERCEL", 13, BLUE, True)
    text(d, (610, 290), "OpenSeek API + Telegram user session", 17, INK, True)
    text(d, (610, 324), "Own Vercel · own Supabase · own credentials", 13, MUTED)
    text(d, (610, 350), "Supabase stores a rebuildable index and queue", 13, MUTED)

    rounded(d, (580, 420, 1012, 602), fill=WHITE, outline="#C2DDD8")
    rounded(d, (604, 442, 988, 482), fill=PALE_TEAL, outline=PALE_TEAL, radius=12)
    text(d, (624, 454), "PARTICIPANT NODE B  ·  LOCAL / VPS", 13, TEAL, True)
    text(d, (610, 506), "OpenSeek API + Telegram user session", 17, INK, True)
    text(d, (610, 540), "Own host · own rebuildable SQLite index", 13, MUTED)
    text(d, (610, 566), "Same client protocol; no Vercel required", 13, MUTED)

    # Telegram pool
    rounded(d, (1172, 244, 1536, 540), fill=WHITE, outline="#E6C59F")
    rounded(d, (1196, 266, 1512, 306), fill=PALE_ORANGE, outline=PALE_ORANGE, radius=12)
    text(d, (1215, 278), "SHARED TELEGRAM SUPERGROUP", 13, ORANGE, True)
    text(d, (1204, 338), "Canonical durable pool", 18, INK, True)
    text(d, (1204, 382), "NVMT tile documents", 15, MUTED)
    text(d, (1204, 412), "Versioned hint manifests", 15, MUTED)
    text(d, (1204, 442), "Each participant posts as their", 14, MUTED)
    text(d, (1204, 466), "own authorized Telegram account", 14, MUTED)
    text(d, (1204, 504), "No per-user bot to add", 13, TEAL, True)

    line(d, [(420, 334), (500, 334), (500, 324), (580, 324)], BLUE)
    line(d, [(1012, 324), (1088, 324), (1088, 356), (1172, 356)], TEAL)
    line(d, [(1172, 420), (1096, 420), (1096, 506), (1012, 506)], ORANGE)
    line(d, [(580, 538), (500, 538), (500, 438), (420, 438)], BLUE)
    text(d, (454, 290), "HTTPS", 12, BLUE, True)
    text(d, (1030, 289), "publish", 12, TEAL, True)
    text(d, (1031, 453), "read", 12, ORANGE, True)
    text(d, (453, 520), "bounded NVMT", 12, BLUE, True)

    rounded(d, (64, 648, 1536, 710), fill="#EAF0F4", outline="#EAF0F4", radius=14)
    text(d, (92, 670), "A pool node can pause or disappear. Nuvio keeps making thumbnails locally.", 15, INK, True)
    save(image, "pool-architecture.png")


def vercel_layout():
    image, d = canvas(
        1600, 920, "Participant Vercel node", "What lives in Vercel, Supabase, and Telegram",
        "The participant's Supabase project is working state. Accepted NVMT packages live in the shared Telegram group.")

    # Nuvio left
    rounded(d, (64, 250, 350, 614), fill=WHITE, outline="#B8D5E7")
    rounded(d, (88, 274, 326, 316), fill=PALE_BLUE, outline=PALE_BLUE, radius=12)
    text(d, (110, 286), "NUVIO", 14, BLUE, True)
    text(d, (92, 346), "POST package hints", 14, INK, True)
    text(d, (92, 374), "PUT bytes to signed", 14, INK, True)
    text(d, (92, 398), "Supabase staging URL", 14, INK, True)
    text(d, (92, 454), "GET exact / hint", 14, MUTED)
    text(d, (92, 482), "GET manifest + chunks", 14, MUTED)
    text(d, (92, 538), "Paired device token", 12, MUTED)
    text(d, (92, 562), "TLS verified by platform", 12, MUTED)

    # Vercel
    rounded(d, (450, 208, 910, 674), fill=WHITE, outline="#B8D5E7")
    rounded(d, (476, 234, 884, 278), fill=PALE_BLUE, outline=PALE_BLUE, radius=12)
    text(d, (500, 246), "PARTICIPANT'S VERCEL PROJECT", 14, BLUE, True)
    text(d, (484, 310), "OpenSeek functions", 19, INK, True)
    text(d, (484, 348), "Pairing · exact lookup · hint ranking", 14, MUTED)
    text(d, (484, 376), "NVMT validation · bounded tile reads", 14, MUTED)
    text(d, (484, 404), "Telegram read / publish operations", 14, MUTED)
    rounded(d, (484, 450, 876, 526), fill=PALE_ORANGE, outline="#F0DFC8", radius=12)
    text(d, (502, 464), "Upload path", 13, ORANGE, True)
    text(d, (502, 489), "signed stage → validate → Telegram → status", 12, INK)
    rounded(d, (484, 548, 876, 630), fill=PALE_TEAL, outline="#C5DFDA", radius=12)
    text(d, (502, 562), "Read path", 13, TEAL, True)
    text(d, (502, 587), "Telegram → verify → bounded response", 12, INK)

    # Supabase
    rounded(d, (1000, 208, 1330, 674), fill=WHITE, outline="#C2DDD8")
    rounded(d, (1024, 234, 1306, 278), fill=PALE_TEAL, outline=PALE_TEAL, radius=12)
    text(d, (1044, 246), "PARTICIPANT'S SUPABASE", 14, TEAL, True)
    text(d, (1030, 310), "Postgres · rebuildable", 17, INK, True)
    for y, label in [(356, "pool_channel_tiles"), (394, "pool_hints"),
                     (432, "pool_channel_state"), (470, "pool_uploads"),
                     (508, "pool_publication_queue"), (546, "pool_devices · invites"),
                     (584, "Telegram session lease")]:
        rounded(d, (1030, y, 1300, y + 30), fill="#F4F7F9", outline="#E5EBEF", radius=8)
        text(d, (1042, y + 6), label, 11, MUTED, True)

    # Telegram
    rounded(d, (1390, 250, 1538, 614), fill=WHITE, outline="#E6C59F")
    rounded(d, (1408, 274, 1520, 316), fill=PALE_ORANGE, outline=PALE_ORANGE, radius=12)
    text(d, (1421, 286), "TELEGRAM", 12, ORANGE, True)
    text(d, (1410, 348), "Accepted", 14, INK, True)
    text(d, (1410, 376), "NVMT tiles", 13, MUTED)
    text(d, (1410, 402), "and hints", 13, MUTED)
    text(d, (1410, 470), "Durable", 14, INK, True)
    text(d, (1410, 498), "pool data", 13, MUTED)
    text(d, (1410, 538), "Source of", 13, MUTED)
    text(d, (1410, 562), "truth", 13, MUTED)

    # Storage bucket under Supabase
    rounded(d, (1000, 714, 1330, 836), fill=WHITE, outline="#E6C59F")
    rounded(d, (1024, 734, 1306, 774), fill=PALE_ORANGE, outline=PALE_ORANGE, radius=12)
    text(d, (1043, 745), "PRIVATE STORAGE BUCKET", 12, ORANGE, True)
    text(d, (1030, 790), "Temporary upload staging · 16 MiB max", 11, INK)
    text(d, (1030, 812), "Removed after publish or cleanup", 11, MUTED)

    line(d, [(350, 372), (400, 372), (400, 486), (450, 486)], BLUE)
    line(d, [(450, 576), (400, 576), (400, 520), (350, 520)], TEAL)
    line(d, [(910, 380), (956, 380), (956, 404), (1000, 404)], TEAL)
    line(d, [(1000, 470), (956, 470), (956, 548), (910, 548)], TEAL)
    line(d, [(910, 470), (950, 470), (950, 780), (1000, 780)], ORANGE)
    line(d, [(1330, 404), (1360, 404), (1360, 432), (1390, 432)], ORANGE)
    line(d, [(1390, 482), (1360, 482), (1360, 548), (1330, 548)], BLUE)

    rounded(d, (64, 714, 910, 836), fill="#EAF0F4", outline="#EAF0F4", radius=14)
    text(d, (90, 738), "Bounds that keep the free-tier path predictable", 15, INK, True)
    text(d, (90, 772), "16 MiB max NVMT package · 256 KiB max tile · at most 12 tiles per read", 13, MUTED)
    text(d, (90, 800), "Responses stay below the 4.5 MB Vercel cap; larger work is split into chunks.", 13, MUTED)
    save(image, "participant-vercel-layout.png")


def lifecycle():
    image, d = canvas(
        1600, 850, "Thumbnail lifecycle", "One opt-in contribution improves later previews",
        "NVMT contains generated JPEG tiles and timing metadata; source video, stream URL, and source headers stay on the client.")

    cards = [(64, 210, 500, 620), (550, 210, 1036, 620), (1086, 210, 1536, 620)]
    for box, fill, edge in zip(cards, [PALE_BLUE, PALE_TEAL, PALE_ORANGE], ["#B8D5E7", "#C2DDD8", "#E6C59F"]):
        rounded(d, box, fill=WHITE, outline=edge)
    for x, number, label, color, fill in [
        (88, "01", "CONTRIBUTE", BLUE, PALE_BLUE),
        (574, "02", "FIND", TEAL, PALE_TEAL),
        (1110, "03", "USE + COMPLETE", ORANGE, PALE_ORANGE),
    ]:
        rounded(d, (x, 234, x + 390, 282), fill=fill, outline=fill, radius=12)
        text(d, (x + 16, 245), number, 13, color, True)
        text(d, (x + 60, 245), label, 13, color, True)

    text(d, (92, 316), "Nuvio creates thumbnails", 18, INK, True)
    text(d, (92, 354), "The local decoder keeps running", 14, MUTED)
    text(d, (92, 380), "even if the pool is offline.", 14, MUTED)
    # Partial tile strip
    for i, x in enumerate([94, 148, 202, 256, 310, 364]):
        fill = ["#5B9CC9", "#76B3D5", "#D8E5EC", "#168F83", "#C2D8D4", "#E0E8EC"][i]
        rounded(d, (x, 432, x + 42, 468), fill=fill, outline=WHITE, radius=5)
    text(d, (96, 482), "Finished NVMT + allowed hints", 12, MUTED)
    rounded(d, (92, 524, 454, 580), fill=PALE_ORANGE, outline="#F0DFC8", radius=12)
    text(d, (108, 540), "After one sharing opt-in", 13, ORANGE, True)
    text(d, (108, 560), "Nuvio submits automatically", 12, INK)

    text(d, (578, 316), "Node checks exact identity", 18, INK, True)
    text(d, (578, 354), "1. exact fp + NVMT profile", 14, MUTED)
    text(d, (578, 382), "2. hint-ranked candidates on miss", 14, MUTED)
    text(d, (578, 430), "Vercel upload: signed temporary stage", 12, MUTED)
    text(d, (578, 452), "Direct node: bounded NVMT upload", 12, MUTED)
    rounded(d, (578, 504, 1008, 580), fill=PALE_TEAL, outline="#C5DFDA", radius=12)
    text(d, (594, 520), "Publish to Telegram as this participant", 13, TEAL, True)
    text(d, (594, 542), "Queued work retries within provider limits", 12, INK)

    text(d, (1114, 316), "Nuvio fetches bounded chunks", 18, INK, True)
    text(d, (1114, 354), "Checks chunk SHA-256 + NVMT/JPEG structure", 12, MUTED)
    # Another sparse tile strip
    for i, x in enumerate([1118, 1172, 1226, 1280, 1334, 1388]):
        fill = ["#168F83", "#D8E5EC", "#2876B8", "#D8E5EC", "#D8E5EC", "#168F83"][i]
        rounded(d, (x, 410, x + 42, 446), fill=fill, outline=WHITE, radius=5)
    text(d, (1118, 458), "Shared tiles retained · gaps decoded locally", 12, MUTED)
    rounded(d, (1114, 504, 1508, 580), fill=PALE_BLUE, outline="#D2E4EF", radius=12)
    text(d, (1130, 520), "Exact first · hint fallback second", 13, BLUE, True)
    text(d, (1130, 542), "Pool errors never block playback", 12, INK)

    line(d, [(500, 414), (550, 414)], BLUE)
    line(d, [(1036, 414), (1086, 414)], TEAL)
    rounded(d, (64, 670, 1536, 778), fill="#EAF0F4", outline="#EAF0F4", radius=14)
    text(d, (92, 692), "Honest zero-spend behavior", 15, INK, True)
    text(d, (92, 724), "Vercel, Supabase, and Telegram quotas can pause a node. Uploads remain pending or retry; nothing upgrades a plan.", 13, MUTED)
    text(d, (92, 750), "Local thumbnails remain available while the shared pool is paused, full, or unreachable.", 13, MUTED)
    save(image, "thumbnail-lifecycle.png")


if __name__ == "__main__":
    architecture()
    vercel_layout()
    lifecycle()
