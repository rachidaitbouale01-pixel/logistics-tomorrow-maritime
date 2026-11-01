"""
Logistics Tomorrow - AI podcast on autopilot (v2, free).
Sheet topic + your notes -> Gemini script -> human-like voices (Gemini TTS, fallback edge-tts)
-> branded cover (AI art or your template + real title text) -> RSS on GitHub Pages
-> Spotify & Apple Podcasts read the RSS automatically.
"""
import asyncio, base64, csv, io, json, os, re, subprocess, sys, time, uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ------------------------------------------------------------------ settings
E = os.environ.get
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SHEET_CSV_URL  = os.environ["SHEET_CSV_URL"]
BASE_URL       = os.environ["BASE_URL"].rstrip("/")
LANG           = E("PODCAST_LANG") or "en"
TITLE          = E("PODCAST_TITLE") or "Logistics Tomorrow"
AUTHOR         = E("PODCAST_AUTHOR") or "Rachid Ait Bouale"
EMAIL          = E("PODCAST_EMAIL") or "you@example.com"
DESCRIPTION    = E("PODCAST_DESCRIPTION") or "AI-produced episodes on supply chain, SAP and smart logistics."
CATEGORY       = E("PODCAST_CATEGORY") or "Business"
MODEL          = E("GEMINI_MODEL") or "gemini-flash-latest"
TTS_MODEL      = E("GEMINI_TTS_MODEL") or ""          # empty = auto-detect
HOST_A         = E("HOST_A") or "Alex"                # curious host
HOST_B         = E("HOST_B") or "Sara"                # expert host
VOICE_A        = E("GEMINI_VOICE_A") or "Puck"        # upbeat male
VOICE_B        = E("GEMINI_VOICE_B") or "Kore"        # firm female
CREATOR_BIO    = E("CREATOR_BIO") or ("Rachid Ait Bouale, SAP S/4HANA consultant (Production Planning, "
                                       "Maintenance, Quality) and PhD researcher in supply chain")
CTA            = E("PODCAST_CTA") or "Subscribe to the Logistics Tomorrow newsletter for the full breakdown."
MINUTES        = int(E("EPISODE_MINUTES") or "12")
USE_AI_COVER   = (E("USE_AI_COVER") or "yes").lower() in ("yes", "true", "1", "oui")
COVER_STYLE    = E("COVER_STYLE") or ("cinematic isometric digital illustration, smart port and global supply "
                  "chain, containers, cranes, glowing data network lines, deep navy and teal palette with "
                  "orange accents, dusk lighting, highly detailed")
ACCENT         = E("BRAND_COLOR") or "#FF7A45"
LINKS = [(name, (E(var) or "").strip()) for name, var in [
    ("LinkedIn", "LINK_LINKEDIN"), ("Newsletter (beehiiv)", "LINK_BEEHIIV"), ("Substack", "LINK_SUBSTACK"),
    ("Spotify", "LINK_SPOTIFY"), ("Apple Podcasts", "LINK_APPLE"), ("YouTube", "LINK_YOUTUBE")]]
LINKS = [(n, u) for n, u in LINKS if u]


def links_text():
    if not LINKS:
        return ""
    return "\n\nFollow Rachid Ait Bouale:\n" + "\n".join(f"{n}: {u}" for n, u in LINKS)

EDGE_VOICES = {
    "en": ("en-US-AndrewMultilingualNeural", "en-US-AvaMultilingualNeural"),
    "fr": ("fr-FR-RemyMultilingualNeural", "fr-FR-VivienneMultilingualNeural"),
    "ar": ("ar-MA-JamalNeural", "ar-MA-MounaNeural"),
}
LANG_NAME = {"en": "English", "fr": "French", "ar": "Arabic"}
API = "https://generativelanguage.googleapis.com/v1beta"
HDR = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
DB, EP_DIR, BRAND = "episodes.json", "episodes", "brand"


# ------------------------------------------------------------------ helpers
def gemini_post(model, body, tries=5, timeout=300):
    """POST with retries on busy/limit errors. Returns response (may be not ok)."""
    r = None
    for attempt in range(tries):
        r = requests.post(f"{API}/models/{model}:generateContent", headers=HDR, json=body, timeout=timeout)
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 20 * (attempt + 1)
            print(f"  {model} busy ({r.status_code}), retry in {wait}s")
            time.sleep(wait)
            continue
        break
    return r


def list_models():
    try:
        r = requests.get(f"{API}/models?pageSize=1000", headers=HDR, timeout=30)
        r.raise_for_status()
        return [m["name"].split("/", 1)[1] for m in r.json().get("models", [])]
    except Exception as exc:
        print(f"  could not list models: {exc}")
        return []


def pick(models, must, prefer=("flash",), avoid=()):
    c = [m for m in models if all(k in m for k in must) and not any(a in m for a in avoid)]
    c.sort(key=lambda m: (not any(p in m for p in prefer), "preview" in m, m), reverse=False)
    return c


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def font(size):
    for p in [f"{BRAND}/font.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ------------------------------------------------------------------ sheet
def load_db():
    with open(DB, encoding="utf-8") as f:
        return json.load(f)


def next_topic(db):
    done = {e["topic"].strip().lower() for e in db["episodes"]}
    text = requests.get(SHEET_CSV_URL, timeout=30).content.decode("utf-8")
    if "<html" in text[:500].lower():
        raise RuntimeError("SHEET_CSV_URL returns a web page, not CSV.")
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        topic = row.get("topic", "")
        if topic and row.get("approved", "").lower() in ("yes", "y", "oui", "1") and topic.lower() not in done:
            return topic, row.get("notes", "")
    return None, ""


# ------------------------------------------------------------------ script
def write_script(topic, notes, n):
    words = MINUTES * 150
    prompt = f"""You are the head writer of the podcast "{TITLE}", created and curated by {CREATOR_BIO}.
Write episode {n} as a natural, unscripted-sounding conversation in {LANG_NAME[LANG]} between two AI hosts:
- {HOST_A}: curious, warm, asks the questions listeners would ask, uses concrete everyday analogies.
- {HOST_B}: sharp practitioner, explains with real industry examples (manufacturing, ports, ERP/SAP, quality, maintenance).

Topic: {topic}
Rachid's own notes and angle (the core of the episode; attribute them as "Rachid's take" or "Rachid's experience"):
{notes or "(none - build a strong practitioner angle yourself)"}

Make it sound HUMAN:
- Short turns (1-3 sentences), frequent back-and-forth, contractions, natural reactions
  ("Right.", "Wait, really?", "Exactly.", "Hmm, that's interesting.") and occasional light humour.
- One clear structure: hook in the first 2 lines -> why it matters -> 3 key ideas with examples -> practical takeaways.
- Opening: {HOST_A} welcomes listeners to "{TITLE}" and says it is produced by Rachid Ait Bouale.
- Closing: quick recap of 3 takeaways, then: "{CTA}"
- About {words} words. Be accurate; never invent statistics, company names or quotes. If uncertain, say so.
- No sound effects, stage directions, emojis or markdown.

Return ONLY JSON:
{{"title": "catchy episode title, max 60 chars",
  "description": "3-4 sentence show-notes summary, end with the call to action",
  "cover_text": "2-5 punchy words for the cover",
  "image_prompt": "one sentence describing a visual metaphor for this episode, no text in the image",
  "dialogue": [{{"speaker": "A", "text": "..."}}, {{"speaker": "B", "text": "..."}}]}}"""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    for model in [MODEL] + [m for m in ("gemini-flash-latest", "gemini-flash-lite-latest") if m != MODEL]:
        r = gemini_post(model, body)
        if r.ok:
            print(f"Script written with {model}")
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```json|```$", "", txt.strip()).strip())
        print(f"  {model} failed ({r.status_code})")
    r.raise_for_status()


# ------------------------------------------------------------------ voices
def chunk_dialogue(dialogue, max_chars=2400):
    chunks, cur, size = [], [], 0
    for line in dialogue:
        t = line["text"].strip()
        if not t:
            continue
        if cur and size + len(t) > max_chars:
            chunks.append(cur); cur, size = [], 0
        cur.append(line); size += len(t)
    if cur:
        chunks.append(cur)
    return chunks


def gemini_tts(dialogue, out_wav, models):
    """Multi-speaker Gemini TTS (most human-like). Returns True on success."""
    for model in models:
        print(f"Trying Gemini voices with {model}")
        pcm = bytearray()
        ok = True
        for i, chunk in enumerate(chunk_dialogue(dialogue)):
            convo = "\n".join(f"{HOST_A if l['speaker'].upper()=='A' else HOST_B}: {l['text'].strip()}" for l in chunk)
            body = {
                "contents": [{"parts": [{"text":
                    f"Read this podcast conversation between {HOST_A} and {HOST_B}. Relaxed, warm, "
                    f"conversational pace, natural pauses, genuine reactions:\n{convo}"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                        {"speaker": HOST_A, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_A}}},
                        {"speaker": HOST_B, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_B}}},
                    ]}}}}
            r = gemini_post(model, body, tries=4)
            try:
                r.raise_for_status()
                part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                pcm += base64.b64decode(part["data"])
                pcm += b"\x00\x00" * int(24000 * 0.4)        # 0.4 s pause between chunks
                print(f"  chunk {i+1} ok")
            except Exception as exc:
                print(f"  chunk {i+1} failed: {r.status_code if r is not None else ''} {str(exc)[:150]}")
                ok = False
                break
        if ok and pcm:
            with open("/tmp/voice.pcm", "wb") as f:
                f.write(pcm)
            ffmpeg("-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "/tmp/voice.pcm", out_wav)
            return True
    return False


async def edge_tts_voice(dialogue, out_wav, workdir):
    import edge_tts
    va, vb = EDGE_VOICES.get(LANG, EDGE_VOICES["en"])
    sil = os.path.join(workdir, "sil.mp3")
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.35", "-b:a", "48k", sil)
    parts = []
    for i, line in enumerate(dialogue):
        t = line["text"].strip()
        if not t:
            continue
        a = line["speaker"].upper() == "A"
        p = os.path.join(workdir, f"seg_{i:04d}.mp3")
        await edge_tts.Communicate(t, va if a else vb, rate="+4%" if a else "+0%").save(p)
        parts += [p, sil]
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    ffmpeg("-f", "concat", "-safe", "0", "-i", lst, out_wav)


def make_audio(dialogue, out_mp3, workdir):
    voice = os.path.join(workdir, "voice.wav")
    models = [TTS_MODEL] if TTS_MODEL else pick(list_models(), ["tts"])
    if not (models and gemini_tts(dialogue, voice, models)):
        if (E("LOCK_VOICES") or "no").lower() in ("yes", "1", "true"):
            raise RuntimeError("Gemini voices unavailable - episode skipped to keep host voices consistent")
        print("Using edge-tts voices (fallback)")
        asyncio.run(edge_tts_voice(dialogue, voice, workdir))
    # optional branded intro/outro music: brand/intro.mp3, brand/outro.mp3
    inputs = [p for p in (f"{BRAND}/intro.mp3", voice, f"{BRAND}/outro.mp3") if os.path.exists(p)]
    args, labels = [], ""
    for i, p in enumerate(inputs):
        args += ["-i", p]
        labels += f"[{i}:a]aresample=44100,aformat=channel_layouts=mono[a{i}];"
    chain = "".join(f"[a{i}]" for i in range(len(inputs)))
    ffmpeg(*args, "-filter_complex",
           f"{labels}{chain}concat=n={len(inputs)}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11[out]",
           "-map", "[out]", "-ac", "1", "-b:a", "96k", out_mp3)


def duration_seconds(mp3):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp3],
                       capture_output=True, text=True, check=True)
    return int(float(o.stdout.strip()))


# ------------------------------------------------------------------ covers
def square(img, size=3000):
    img = img.convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    return img.resize((size, size), Image.LANCZOS)


def ai_art(image_prompt):
    models = pick(list_models(), ["image"], prefer=("flash",), avoid=("imagen", "tts"))
    prompt = (f"Square podcast cover artwork. {image_prompt}. Style: {COVER_STYLE}. "
              "Leave the lower third darker and simple. Absolutely no text, letters, numbers or logos.")
    for model in models[:3]:
        print(f"Trying AI cover art with {model}")
        r = gemini_post(model, {"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}, tries=3)
        try:
            r.raise_for_status()
            for p in r.json()["candidates"][0]["content"]["parts"]:
                if "inlineData" in p:
                    return Image.open(io.BytesIO(base64.b64decode(p["inlineData"]["data"])))
        except Exception as exc:
            print(f"  cover art failed: {str(exc)[:150]}")
    return None


def gradient_bg(size=3000):
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)], fill=(int(10 + 10 * t), int(40 + 30 * t), int(80 + 30 * t)))
    for i in range(0, size, 150):
        d.line([(i, 0), (0, i)], fill=(30, 90, 120), width=3)
    return img


def wrap(d, text, max_w, start=300, min_size=150, max_lines=3):
    size = start
    while size >= min_size:
        f, lines, cur = font(size), [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if d.textlength(t, font=f) > max_w and cur:
                lines.append(cur); cur = w
            else:
                cur = t
        lines.append(cur)
        if len(lines) <= max_lines:
            return f, lines, size
        size -= 20
    return f, lines, size


def make_cover(path, headline, episode_label="", image_prompt=None):
    S = 3000
    base = ai_art(image_prompt) if (USE_AI_COVER and image_prompt) else None
    if base is None and os.path.exists(f"{BRAND}/template.png"):
        base = Image.open(f"{BRAND}/template.png")
    img = square(base) if base is not None else gradient_bg(S)

    # dark fade at the bottom for readable text
    fade = Image.new("L", (1, S))
    for y in range(S):
        fade.putpixel((0, y), int(min(1, max(0, (y - S * 0.30) / (S * 0.45))) ** 0.9 * 238))
    shade = Image.new("RGB", (S, S), (6, 14, 30))
    img = Image.composite(shade, img, fade.resize((S, S)))
    d = ImageDraw.Draw(img)

    # show name band (top) + logo
    x0 = 170
    if os.path.exists(f"{BRAND}/logo.png"):
        logo = Image.open(f"{BRAND}/logo.png").convert("RGBA")
        logo.thumbnail((420, 420))
        img.paste(logo, (x0, 150), logo)
        x0 += logo.width + 50
    tsize = 130
    while tsize > 60 and d.textlength(TITLE.upper(), font=font(tsize)) > S - x0 - 200:
        tsize -= 5
    d.rounded_rectangle([x0 - 30, 170, x0 + d.textlength(TITLE.upper(), font=font(tsize)) + 30, 170 + tsize + 50],
                        radius=40, fill=(6, 14, 30))
    d.text((x0, 185), TITLE.upper(), font=font(tsize), fill="white")

    # episode label + headline
    f, lines, size = wrap(d, headline.upper(), S - 340)
    y = S - 330 - len(lines) * (size + 40)
    if episode_label:
        lf = font(110)
        w = d.textlength(episode_label, font=lf)
        d.rounded_rectangle([170, y - 210, 170 + w + 80, y - 50], radius=30, fill=ACCENT)
        d.text((210, y - 195), episode_label, font=lf, fill="white")
    for ln in lines:
        d.text((170, y), ln, font=f, fill="white", stroke_width=4, stroke_fill=(6, 14, 30))
        y += size + 40
    d.rectangle([170, S - 240, 620, S - 220], fill=ACCENT)
    d.text((170, S - 190), AUTHOR, font=font(100), fill=(220, 230, 240))
    img.save(path, "PNG", optimize=True)


def show_cover():
    if os.path.exists(f"{BRAND}/show_cover.png"):
        square(Image.open(f"{BRAND}/show_cover.png")).save("cover.png", "PNG", optimize=True)
    elif not os.path.exists("cover.png"):
        make_cover("cover.png", "Supply chain. SAP. The future of logistics.")


# ------------------------------------------------------------------ feed
def build_feed(db):
    feed_id = re.sub(r"^https?://", "", f"{BASE_URL}/feed.xml").rstrip("/")
    show_guid = uuid.uuid5(uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6"), feed_id)
    items = []
    for e in reversed(db["episodes"]):
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['description'] + links_text())}</description>
      <itunes:summary>{escape(e['description'] + links_text())}</itunes:summary>
      <enclosure url="{BASE_URL}/{e['audio']}" length="{e['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{e['id']}</guid>
      <pubDate>{e['pubDate']}</pubDate>
      <itunes:duration>{e['duration']}</itunes:duration>
      <itunes:image href="{BASE_URL}/{e['cover']}"/>
      <itunes:episode>{e.get('number', '')}</itunes:episode>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
      <link>{BASE_URL}/</link>{transcript_tag(e)}
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{escape(TITLE)}</title>
    <link>{BASE_URL}</link>
    <atom:link href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    <language>{LANG}</language>
    <description>{escape(DESCRIPTION + links_text())}</description>
    <itunes:summary>{escape(DESCRIPTION + links_text())}</itunes:summary>
    <itunes:author>{escape(AUTHOR)}</itunes:author>
    <itunes:owner><itunes:name>{escape(AUTHOR)}</itunes:name><itunes:email>{escape(EMAIL)}</itunes:email></itunes:owner>
    <itunes:image href="{BASE_URL}/cover.png"/>
    <itunes:category text="{escape(CATEGORY)}"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <podcast:locked owner="{escape(EMAIL)}">yes</podcast:locked>
    <podcast:guid>{show_guid}</podcast:guid>
{chr(10).join(items)}
  </channel>
</rss>
"""
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(feed)
    build_site(db)


def transcript_tag(e):
    t = e["audio"].replace(".mp3", ".txt")
    if os.path.exists(t):
        return f'\n      <podcast:transcript url="{BASE_URL}/{t}" type="text/plain"/>'
    return ""


def build_site(db):
    """index.html: branded landing page with all links and the latest episodes."""
    btns = "".join(f'<a class="btn" href="{escape(u)}" target="_blank" rel="noopener">{escape(n)}</a>' for n, u in LINKS)
    btns += f'<a class="btn ghost" href="feed.xml">RSS feed</a>'
    eps = ""
    for e in reversed(db["episodes"][-20:]):
        eps += f"""<article class="ep"><img src="{e['cover']}" alt="" loading="lazy">
<div><h3>{escape(e['title'])}</h3><p>{escape(e['description'])}</p>
<audio controls preload="none" src="{e['audio']}"></audio></div></article>"""
    if not eps:
        eps = "<p>First episode coming soon.</p>"
    html = f"""<!doctype html><html lang="{LANG}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(TITLE)} | {escape(AUTHOR)}</title>
<meta name="description" content="{escape(DESCRIPTION)}">
<meta property="og:title" content="{escape(TITLE)}"><meta property="og:image" content="{BASE_URL}/cover.png">
<link rel="alternate" type="application/rss+xml" title="{escape(TITLE)}" href="feed.xml">
<style>
:root{{--bg:#06101e;--card:#0e1d33;--text:#e8eef6;--muted:#9fb0c6;--accent:{ACCENT}}}
*{{box-sizing:border-box}}body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}}
.wrap{{max-width:900px;margin:0 auto;padding:40px 20px}}
header{{display:flex;gap:28px;align-items:center;flex-wrap:wrap}}
header img{{width:200px;height:200px;border-radius:18px;box-shadow:0 10px 40px #0008}}
h1{{margin:0 0 8px;font-size:2.2rem}}.sub{{color:var(--muted);margin:0 0 18px;line-height:1.5}}
.btn{{display:inline-block;margin:4px 6px 4px 0;padding:10px 16px;border-radius:999px;background:var(--accent);color:#fff;text-decoration:none;font-weight:600}}
.btn.ghost{{background:transparent;border:1px solid var(--muted);color:var(--text)}}
h2{{margin:44px 0 16px}}.ep{{display:flex;gap:18px;background:var(--card);border-radius:16px;padding:16px;margin-bottom:16px}}
.ep img{{width:120px;height:120px;border-radius:10px;flex-shrink:0}}.ep h3{{margin:0 0 6px}}.ep p{{color:var(--muted);margin:0 0 10px;line-height:1.45}}
audio{{width:100%}}@media(max-width:600px){{.ep{{flex-direction:column}}.ep img{{width:100%;height:auto}}}}
footer{{color:var(--muted);font-size:.85rem;margin-top:40px}}
</style></head><body><div class="wrap">
<header><img src="cover.png" alt="{escape(TITLE)} cover"><div>
<h1>{escape(TITLE)}</h1><p class="sub">{escape(DESCRIPTION)}</p>{btns}</div></header>
<h2>Episodes</h2>{eps}
<footer>AI-voiced episodes, created and curated by {escape(AUTHOR)}.</footer>
</div></body></html>"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)


# ------------------------------------------------------------------ main
def main():
    db = load_db()
    os.makedirs(EP_DIR, exist_ok=True)
    show_cover()
    topic, notes = next_topic(db)
    if not topic:
        print("No new approved topic. Nothing to do.")
        build_feed(db)
        return

    n = len(db["episodes"]) + 1
    slug = f"ep{n:03d}"
    print(f"Episode {n}: {topic}")
    ep = write_script(topic, notes, n)
    work = f"/tmp/{slug}"; os.makedirs(work, exist_ok=True)
    audio, cover = f"{EP_DIR}/{slug}.mp3", f"{EP_DIR}/{slug}.png"
    make_audio(ep["dialogue"], audio, work)
    make_cover(cover, ep.get("cover_text") or ep["title"], f"EP {n:02d}", ep.get("image_prompt") or topic)

    db["episodes"].append({
        "id": f"{slug}-{int(time.time())}", "number": n, "topic": topic,
        "title": f"Ep. {n} | {ep['title']}",
        "description": ep["description"] + " | AI-voiced episode, created and curated by " + AUTHOR + ".",
        "audio": audio, "cover": cover, "bytes": os.path.getsize(audio),
        "duration": duration_seconds(audio), "pubDate": format_datetime(datetime.now(timezone.utc)),
    })
    with open(DB, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    with open(f"{EP_DIR}/{slug}.txt", "w", encoding="utf-8") as f:   # transcript = free blog/LinkedIn material
        f.write(f"{ep['title']}\n\n{ep['description']}\n\n")
        f.writelines(f"{HOST_A if l['speaker'].upper()=='A' else HOST_B}: {l['text']}\n\n" for l in ep["dialogue"])
    build_feed(db)
    print(f"Published {slug}: {ep['title']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
