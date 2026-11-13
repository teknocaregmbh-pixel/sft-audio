"""
Swiss Financial Time - ready-made "Heart" audio (Kokoro, free, Apache-2.0).

Runs on GitHub Actions every 30 minutes:
  * reads the latest posts from the Blogger feed,
  * makes a spoken news briefing (one MP3 per headline) and one MP3 per article,
  * writes everything into ./site, which is published with GitHub Pages.
Only new or changed text is turned into speech, so most runs take under a minute.
"""
import hashlib, html, json, os, re, sys, time, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import lameenc
from bs4 import BeautifulSoup
from kokoro_onnx import Kokoro

SITE_URL = os.environ.get('SITE_URL', 'https://www.swissfinancialtime.ch')
SITE = 'Swiss Financial Time'
VOICE, LANG = 'af_heart', 'en-us'
OUT = os.environ.get('OUT', 'site')
MODEL_DIR = os.environ.get('MODEL_DIR', 'model')
KEEP_ARTICLES = int(os.environ.get('KEEP_ARTICLES', '40'))     # newest articles kept online
NEW_PER_RUN = int(os.environ.get('NEW_PER_RUN', '12'))         # max new articles made per run
TZ = ZoneInfo('Europe/Zurich')
UA = {'User-Agent': 'Mozilla/5.0 (sft-audio)'}


# ---------- text: same rules as the reader on the website ----------
def clean(t):
    t = re.sub(r'\s+', ' ', str(t or ''))
    t = re.sub(r'(Graphic|Chart|Photo|Image|Bild|Foto)\s*:\s*[^.]*?(Swiss Financial Time|SFT)\.?', '', t, flags=re.I)
    t = re.sub(r'https?://\S+', '', t)
    def cur(m):
        word = {'USD': 'US dollars', 'CHF': 'Swiss francs', 'EUR': 'euros', 'GBP': 'pounds'}[m.group(1)]
        unit = m.group(4)
        unit = ({'bn': 'billion', 'm': 'million'}.get(unit.lower(), unit) + ' ') if unit else ''
        return m.group(2) + ' ' + unit + word
    t = re.sub(r'\b(USD|CHF|EUR|GBP)\s?(\d+(?:,\d{3})*(?:\.\d+)?)(\s?(bn|billion|m|million|trillion)\b)?', cur, t)
    t = re.sub(r'\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)\s?(bn|billion)\b', r'\1 billion dollars', t, flags=re.I)
    t = re.sub(r'\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)\s?(m|million)\b', r'\1 million dollars', t, flags=re.I)
    t = re.sub(r'\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)', r'\1 dollars', t)
    t = re.sub(r'(\d)\s?%', r'\1 percent', t)
    return re.sub(r'\s+', ' ', t).strip()


ABBR = r'\b(U\.S|U\.K|e\.g|i\.e|Mr|Mrs|Ms|Dr|St|No|vs|Inc|Ltd|Corp|Co|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.'


def sentences(t, mx=0):
    safe = re.sub(r'(\d)\.(\d)', '\\1\u2060\\2', clean(t))
    safe = re.sub(r'(\w)\.(com|ch|org|net|gov|io|de|uk|eu)\b', '\\1\u2060\\2', safe)
    safe = re.sub(ABBR, lambda m: m.group(0)[:-1] + '\u2063', safe)
    parts = re.findall(r'[^.!?]+[.!?]+["\')\]]*|\S[^.!?]*$', safe)
    out = []
    for p in parts:
        p = p.replace('\u2060', '.').replace('\u2063', '.').strip()
        if not p:
            continue
        while len(p) > 220:
            cut = p.rfind(',', 0, 220)
            if cut < 80:
                cut = p.rfind(' ', 0, 220)
            out.append(p[:cut + 1].strip()); p = p[cut + 1:].strip()
        out.append(p)
    return out[:mx] if mx else out


def article_text(content_html):
    soup = BeautifulSoup(content_html or '', 'html.parser')
    # a heading that only introduces a table (e.g. "Key figures") is dropped with the table
    for t in soup.find_all('table'):
        prev = t.find_previous_sibling()
        if prev is None and t.parent is not None and t.parent.name == 'div':
            prev = t.parent.find_previous_sibling()
        if prev is not None and prev.name in ('h2', 'h3', 'h4'):
            prev.decompose()
    for el in soup.select('table,figure,figcaption,img,iframe,script,style,.tr-caption,.tr-caption-container'):
        el.decompose()
    for hd in list(soup.find_all(['h2', 'h3', 'h4', 'p', 'strong', 'b'])):
        if getattr(hd, 'decomposed', False):
            continue
        if not re.match(r'^\s*(sources?|quellen?|references?|further reading)\s*:?\s*$', hd.get_text(), re.I):
            continue
        head = hd.parent if hd.name in ('strong', 'b') and hd.parent is not None and hd.parent.name != '[document]' else hd
        n = head.find_next_sibling()
        while n is not None and (n.name in ('ul', 'ol') or (n.name == 'p' and n.find('a') and len(n.get_text()) < 400)):
            nxt = n.find_next_sibling(); n.decompose(); n = nxt
        head.decompose()
    for el in soup.find_all(['h2', 'h3', 'h4']):
        txt = el.get_text().strip()
        el.append('' if not txt or txt[-1] in '.!?:' else '.')
    for el in soup.find_all(['p', 'li', 'h2', 'h3', 'h4', 'div', 'br']):
        el.append(' \n')
    text = soup.get_text()
    text = re.split(r'Disclaimer:', text, flags=re.I)[0]
    text = re.sub(r'This article is based on the sources listed[^.]*\.', '', text, flags=re.I)
    return html.unescape(text)


# ---------- feeds ----------
def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def post_id(e):
    m = re.search(r'post-(\d+)', e['id']['$t'])
    return m.group(1) if m else None


# ---------- speech ----------
_tts = None
def tts():
    global _tts
    if _tts is None:
        _tts = Kokoro(os.path.join(MODEL_DIR, 'kokoro-v1.0.onnx'), os.path.join(MODEL_DIR, 'voices-v1.0.bin'))
    return _tts


def speak(parts, gap=0.3):
    sr, chunks = 24000, []
    for t in parts:
        t = t.strip()
        if not t:
            continue
        a, sr = tts().create(t, voice=VOICE, speed=1.0, lang=LANG)
        chunks += [a, np.zeros(int(sr * gap), dtype=np.float32)]
    audio = np.concatenate(chunks) if chunks else np.zeros(sr // 2, dtype=np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    enc = lameenc.Encoder()
    enc.set_bit_rate(48); enc.set_in_sample_rate(sr); enc.set_channels(1); enc.set_quality(2)
    return enc.encode(pcm) + enc.flush(), len(audio) / sr


def h(s):
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:12]


def write(rel, data):
    p = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)


def exists(rel):
    return os.path.isfile(os.path.join(OUT, rel))


# ---------- main ----------
def main():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    try:
        old = json.load(open(os.path.join(OUT, 'index.json')))
    except Exception:
        old = {}
    made = 0

    # 1. news briefing: one file per headline, re-used while the text stays the same
    now = datetime.now(TZ)
    greet = 'Good morning' if now.hour < 12 else 'Good afternoon' if now.hour < 18 else 'Good evening'
    day = now.strftime('%A, %-d %B')
    d = get_json(SITE_URL + '/feeds/posts/summary?alt=json&max-results=10')
    entries = (d.get('feed') or {}).get('entry') or []
    items = [{'title': SITE + ' briefing', 'parts': [greet + '.', 'This is the ' + SITE + ' news briefing for ' + day + '.', 'Here are the latest headlines.']}]
    for i, e in enumerate(entries[:8]):
        t = e['title']['$t']
        summ = ' '.join(sentences((e.get('summary') or {}).get('$t', ''), 2))
        lead = 'First, ' if i == 0 else 'And finally, ' if i == 7 else ''
        items.append({'title': t, 'parts': [lead + clean(t) + '.'] + sentences(summ)})
    items.append({'title': 'End of briefing', 'skip': True,
                  'parts': ['That was your ' + SITE + ' briefing. Read the full stories at swiss financial time dot c h.']})
    brief = []
    for it in items:
        rel = 'b/' + h(VOICE + '|' + '|'.join(it['parts'])) + '.mp3'
        if not exists(rel):
            data, secs = speak(it['parts']); write(rel, data); made += 1
            print(f'briefing  {secs:5.1f}s  {it["title"][:70]}', flush=True)
        brief.append({'t': it['title'], 'f': rel, **({'skip': 1} if it.get('skip') else {})})

    # 2. articles: newest first, one file each, re-made only when the text changes
    arts = dict(old.get('articles') or {})
    d = get_json(SITE_URL + '/feeds/posts/default?alt=json&orderby=published&max-results=25')
    fresh = (d.get('feed') or {}).get('entry') or []
    new_count = 0
    for e in fresh:
        pid = post_id(e)
        if not pid:
            continue
        title = e['title']['$t']
        body = article_text((e.get('content') or {}).get('$t', ''))
        parts = sentences(title + '.') + sentences(body)
        key = h(VOICE + '|' + '\n'.join(parts))
        rel = f'a/{pid}-{key}.mp3'
        if exists(rel):
            arts[pid] = {'f': rel, 't': title, 'p': e['published']['$t']}
            continue
        if new_count >= NEW_PER_RUN:
            continue
        data, secs = speak(parts); write(rel, data); made += 1; new_count += 1
        arts[pid] = {'f': rel, 't': title, 'p': e['published']['$t']}
        print(f'article   {secs:5.1f}s  {title[:70]}', flush=True)
    # keep the newest articles only
    keep = sorted(arts.items(), key=lambda kv: kv[1].get('p', ''), reverse=True)[:KEEP_ARTICLES]
    arts = {k: v for k, v in keep if exists(v['f'])}

    # 3. remove files that are no longer used
    used = {b['f'] for b in brief} | {a['f'] for a in arts.values()}
    for folder in ('a', 'b'):
        p = os.path.join(OUT, folder)
        if os.path.isdir(p):
            for name in os.listdir(p):
                if folder + '/' + name not in used:
                    os.remove(os.path.join(p, name))

    same = (made == 0 and old.get('briefing', {}).get('items') == brief
            and {k: v['f'] for k, v in (old.get('articles') or {}).items()} == {k: v['f'] for k, v in arts.items()})
    gh = os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as f:
            f.write('changed=' + ('false' if same else 'true') + '\n')
    if same:
        print(f'nothing new ({len(arts)} articles online), {time.time() - t0:.0f}s', flush=True)
        return 0
    index = {'updated': datetime.now(TZ).isoformat(timespec='seconds'), 'voice': VOICE,
             'briefing': {'day': day, 'items': brief}, 'articles': arts}
    with open(os.path.join(OUT, 'index.json'), 'w') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
    open(os.path.join(OUT, '.nojekyll'), 'w').close()
    print(f'done: {made} new file(s), {len(arts)} articles online, {time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    sys.exit(main())
