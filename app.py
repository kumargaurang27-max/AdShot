"""
AdShot - screenshot Amazon.in / Flipkart search pages in mobile view,
for many keywords x many Indian cities/pincodes, with sponsored (brand ad)
placements outlined.
"""
import asyncio
import hashlib
import io
import os
import re
import shutil
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright

BASE = Path(__file__).parent
SHOTS = Path(os.getenv("SHOT_DIR", "/tmp/shots"))
SHOTS.mkdir(parents=True, exist_ok=True)

MAX_TASKS_PER_JOB = int(os.getenv("MAX_TASKS_PER_JOB", "24"))
MAX_ACTIVE_JOBS = int(os.getenv("MAX_ACTIVE_JOBS", "3"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))
KEEP_HOURS = float(os.getenv("KEEP_HOURS", "3"))
BRAND_SAMPLES = int(os.getenv("BRAND_SAMPLES", "4"))          # looks per screen when a banner carousel is visible
BRAND_INTERVAL_MS = int(os.getenv("BRAND_INTERVAL_MS", "1600"))  # wait between looks
OCR_MAX = int(os.getenv("OCR_MAX", "6"))                      # max banners read by OCR per look
PROXY_URL = os.getenv("PROXY_URL", "").strip()  # e.g. http://user:pass@host:port (Indian IP recommended)

# city -> (pincode, lat, lng)
CITIES = {
    "Delhi": ("110001", 28.6139, 77.2090),
    "Mumbai": ("400001", 18.9388, 72.8354),
    "Bengaluru": ("560001", 12.9716, 77.5946),
    "Hyderabad": ("500001", 17.3850, 78.4867),
    "Chennai": ("600001", 13.0827, 80.2707),
    "Kolkata": ("700001", 22.5726, 88.3639),
    "Pune": ("411001", 18.5204, 73.8567),
    "Ahmedabad": ("380001", 23.0225, 72.5714),
    "Jaipur": ("302001", 26.9124, 75.7873),
    "Lucknow": ("226001", 26.8467, 80.9462),
    "Gurugram": ("122001", 28.4595, 77.0266),
    "Noida": ("201301", 28.5355, 77.3910),
    "Chandigarh": ("160017", 30.7333, 76.7794),
    "Kochi": ("682001", 9.9312, 76.2673),
    "Indore": ("452001", 22.7196, 75.8577),
    "Bhopal": ("462001", 23.2599, 77.4126),
    "Patna": ("800001", 25.5941, 85.1376),
    "Surat": ("395003", 21.1702, 72.8311),
    "Nagpur": ("440001", 21.1458, 79.0882),
    "Coimbatore": ("641001", 11.0168, 76.9558),
}
ALIASES = {
    "bangalore": "Bengaluru", "gurgaon": "Gurugram", "bombay": "Mumbai",
    "new delhi": "Delhi", "calcutta": "Kolkata", "madras": "Chennai",
    "cochin": "Kochi",
}
DEVICES = {"iphone": "iPhone 13", "android": "Pixel 7"}

JOBS: dict = {}
SEM = asyncio.Semaphore(CONCURRENCY)
_pw = None
_browser = None
_browser_lock = asyncio.Lock()


# ---------------------------------------------------------------- browser ---
async def get_browser():
    global _pw, _browser
    async with _browser_lock:
        if _pw is None:
            _pw = await async_playwright().start()
        if _browser is None or not _browser.is_connected():
            opts = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if PROXY_URL:
                opts["proxy"] = {"server": PROXY_URL}
            _browser = await _pw.chromium.launch(**opts)
        return _browser


async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - KEEP_HOURS * 3600
        for jid in list(JOBS):
            if JOBS[jid]["created"] < cutoff:
                JOBS.pop(jid, None)
                shutil.rmtree(SHOTS / jid, ignore_errors=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = asyncio.create_task(cleanup_loop())
    yield
    t.cancel()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


app = FastAPI(title="AdShot", lifespan=lifespan)


# ----------------------------------------------------------------- models ---
class JobRequest(BaseModel):
    keywords: list[str]
    locations: list[str]  # city names or 6-digit pincodes
    platforms: list[str] = ["amazon", "flipkart"]
    device: str = "iphone"
    screens: int = 5
    highlight: bool = True
    brands: list[str] = []  # focus brand(s): keep only screens showing their ad


def resolve_location(raw: str) -> dict:
    raw = raw.strip()
    if re.fullmatch(r"\d{6}", raw):
        return {"label": raw, "pincode": raw, "lat": None, "lng": None}
    key = ALIASES.get(raw.lower(), raw).lower()
    for name, (pin, lat, lng) in CITIES.items():
        if name.lower() == key:
            return {"label": name, "pincode": pin, "lat": lat, "lng": lng}
    raise ValueError(f"'{raw}' is not a known city or 6-digit pincode")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "x"


# ---------------------------------------------------------- page behaviour ---
HIGHLIGHT_JS = """
({hl, brands}) => {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const B = brands.map(b => [b, norm(b)]).filter(x => x[1]);
  const ok = new Set(['sponsored','ad','ads','advertisement','sponsored brand']);
  let uid = window.__adid || 0;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const t = (n.nodeValue || '').trim().toLowerCase();
    if (!ok.has(t)) continue;
    const el = n.parentElement;
    if (!el) continue;
    const r0 = el.getBoundingClientRect();
    if (r0.width === 0 || r0.height === 0) continue;
    let c = el;
    for (let i = 0; i < 8 && c.parentElement && c.parentElement !== document.body; i++) {
      const r = c.getBoundingClientRect();
      if ((r.width >= 140 && r.height >= 140) || (r.width >= 300 && r.height >= 80)) break;
      c = c.parentElement;
    }
    if (c.hasAttribute('data-adshot')) continue;
    c.setAttribute('data-adshot', '1');
    c.setAttribute('data-adid', String(++uid));
  }
  window.__adid = uid;
  const vh = window.innerHeight, vw = window.innerWidth, out = [];
  document.querySelectorAll('[data-adshot]').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 40 || r.top >= vh - 40) return;
    let hay = e.innerText || '';
    e.querySelectorAll('img[alt],[aria-label],[title],a[href]').forEach(x => {
      hay += ' ' + (x.getAttribute('alt') || '') + ' ' + (x.getAttribute('aria-label') || '') +
             ' ' + (x.getAttribute('title') || '') + ' ' + (x.getAttribute('href') || '');
    });
    const h = norm(hay);
    const hits = B.filter(([b, nb]) => h.includes(nb)).map(x => x[0]);
    if (hl) {
      e.style.outline = hits.length ? '5px solid #00c853' : '3px solid #ff2d55';
      e.style.outlineOffset = '-3px';
    }
    const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top);
    out.push({id: e.getAttribute('data-adid'), w: Math.round(r.width), hits,
              box: [x0, y0, Math.min(r.right, vw) - x0, Math.min(r.bottom, vh) - y0]});
  });
  return {total: document.querySelectorAll('[data-adshot]').length, visible: out};
}
"""

MARK_BRAND_JS = """
(id) => {
  const e = document.querySelector('[data-adid="' + id + '"]');
  if (e) { e.style.outline = '5px solid #00c853'; e.style.outlineOffset = '-3px'; }
}
"""

# Position of the page (window + biggest inner scroller), so we can tell if scrolling worked.
POS_JS = """
() => {
  let inner = 0;
  document.querySelectorAll('div,main,section').forEach(e => {
    if (e.scrollHeight - e.clientHeight > 200) {
      const o = getComputedStyle(e).overflowY;
      if ((o === 'auto' || o === 'scroll') && e.scrollTop > inner) inner = e.scrollTop;
    }
  });
  return Math.round(window.scrollY) + ':' + Math.round(inner);
}
"""

SCROLLBY_JS = """
(dy) => {
  window.scrollBy(0, dy);
  let best = null, bestRoom = 0;
  document.querySelectorAll('div,main,section').forEach(e => {
    const o = getComputedStyle(e).overflowY;
    const room = e.scrollHeight - e.clientHeight;
    if (room > 200 && (o === 'auto' || o === 'scroll') &&
        e.clientHeight > window.innerHeight * 0.5 && room > bestRoom) { best = e; bestRoom = room; }
  });
  if (best) best.scrollBy(0, dy);
}
"""

TOP_JS = """
() => {
  window.scrollTo(0, 0);
  document.querySelectorAll('div,main,section').forEach(e => { if (e.scrollTop > 0) e.scrollTop = 0; });
}
"""


async def scroll_step(page, dy: int) -> bool:
    """Scroll down one screen. Works for window scrolling and inner scroll containers.
    Returns False when the page cannot scroll any further."""
    before = await page.evaluate(POS_JS)
    vp = page.viewport_size
    try:
        await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        await page.mouse.wheel(0, dy)
    except Exception:
        pass
    await page.wait_for_timeout(900)
    if await page.evaluate(POS_JS) != before:
        return True
    await page.evaluate(SCROLLBY_JS, dy)
    await page.wait_for_timeout(900)
    return (await page.evaluate(POS_JS)) != before


async def amazon_flow(page, kw: str, loc: dict) -> str:
    await page.goto("https://www.amazon.in/", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(1500)
    note = "pincode set"
    try:
        await page.locator(
            "#nav-global-location-popover-link, #glow-ingress-block, #nav-global-location-slot"
        ).first.click(timeout=8000)
        await page.locator("#GLUXZipUpdateInput").fill(loc["pincode"], timeout=8000)
        await page.locator("#GLUXZipUpdate input[type=submit], #GLUXZipUpdate").first.click(timeout=5000)
        await page.wait_for_timeout(1500)
        done = page.locator("button[name='glowDoneButton'], #GLUXConfirmClose, .a-popover-footer button")
        if await done.count():
            await done.first.click(timeout=3000)
        await page.wait_for_timeout(800)
    except Exception:
        note = "could not set pincode (showing default location)"
    await page.goto(
        f"https://www.amazon.in/s?k={quote_plus(kw)}", wait_until="domcontentloaded", timeout=45000
    )
    return note


async def flipkart_flow(page, kw: str, loc: dict) -> str:
    await page.goto(
        f"https://www.flipkart.com/search?q={quote_plus(kw)}",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(2000)
    try:  # dismiss login popup if any
        await page.keyboard.press("Escape")
        close = page.locator("button:has-text('✕')")
        if await close.count():
            await close.first.click(timeout=2000)
    except Exception:
        pass
    note = "pincode set"
    try:
        try:
            await page.get_by_text(re.compile(r"pincode|deliver to", re.I)).first.click(timeout=4000)
        except Exception:
            pass
        box = page.locator(
            "input[placeholder*='pincode' i], input[name='pincode'], input[maxlength='6']"
        ).first
        await box.fill(loc["pincode"], timeout=4000)
        await box.press("Enter")
        await page.wait_for_timeout(2000)
    except Exception:
        note = "could not set pincode (showing default location)"
    return note


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def fuzzy_has(text: str, brand: str) -> bool:
    """Does OCR text contain the brand (tolerating small OCR slips)?"""
    nb = _norm(brand)
    if not nb:
        return False
    if nb in _norm(text):
        return True
    if len(nb) < 5:
        return False
    words = re.findall(r"[a-z0-9]+", text.lower())
    cands = words + [a + b for a, b in zip(words, words[1:])]
    return any(
        abs(len(c) - len(nb)) <= 2 and SequenceMatcher(None, nb, c).ratio() >= 0.78 for c in cands
    )


OCR_STATE = {"ok": True}


def ocr_crop(png: bytes, box, dpr: float, cache: dict, key_id: str) -> str:
    """Read the text inside one ad banner (cropped from a viewport screenshot)."""
    try:
        from PIL import Image, ImageOps
        import pytesseract
        im = Image.open(io.BytesIO(png)).convert("RGB")
        x, y, w, h = [int(v * dpr) for v in box]
        im = im.crop((max(0, x), max(0, y), min(im.width, x + w), min(im.height, y + h)))
        if im.width < 4 or im.height < 4:
            return ""
        key = (key_id, hashlib.md5(im.tobytes()).hexdigest())
        if key in cache:
            return cache[key]
        if im.width < 1000:
            im = im.resize((im.width * 2, im.height * 2))
        im = ImageOps.autocontrast(ImageOps.grayscale(im))
        text = pytesseract.image_to_string(im, config="--psm 11")
        cache[key] = text
        return text
    except Exception:
        OCR_STATE["ok"] = False
        return ""


async def find_brand_hits(page, res: dict, brands: list, highlight: bool, dpr: float, cache: dict) -> set:
    """Brands whose ad is visible right now: first from page text, then by reading banner images."""
    hits = set()
    for c in res["visible"]:
        hits.update(c["hits"])
    if hits:
        return hits
    todo = [c for c in res["visible"] if c["box"][2] >= 120 and c["box"][3] >= 50][:OCR_MAX]
    if not todo:
        return hits
    png = await page.screenshot(type="png")
    for c in todo:
        text = await asyncio.to_thread(ocr_crop, png, c["box"], dpr, cache, c["id"])
        got = [b for b in brands if fuzzy_has(text, b)]
        if got:
            hits.update(got)
            if highlight:
                await page.evaluate(MARK_BRAND_JS, c["id"])
    return hits


async def capture(page, jdir: Path, task: dict, highlight: bool, screens: int, brands: list):
    await page.wait_for_timeout(2500)
    title = (await page.title() or "").lower()
    body = (await page.content()).lower()
    if "robot check" in title or "captcha" in body or "enter the characters you see" in body:
        raise RuntimeError("Blocked by a bot check (CAPTCHA). Use an Indian proxy via PROXY_URL.")

    vh = page.viewport_size["height"]
    step = int(vh * 0.8)
    dpr = float(await page.evaluate("window.devicePixelRatio") or 1)
    cache: dict = {}
    saved, total, found = 0, 0, set()
    for i in range(screens):
        if i > 0 and not await scroll_step(page, step):
            break  # reached the end of the page
        await page.wait_for_timeout(700)
        hits: set = set()
        looks = 0
        while True:
            res = await page.evaluate(HIGHLIGHT_JS, {"hl": highlight, "brands": brands})
            total = res["total"]
            if not brands:
                break
            hits = await find_brand_hits(page, res, brands, highlight, dpr, cache)
            looks += 1
            has_banner = any(c["w"] >= 300 for c in res["visible"])  # banners may be carousels
            if hits or not has_banner or looks >= BRAND_SAMPLES:
                break
            await page.wait_for_timeout(BRAND_INTERVAL_MS)  # wait for the carousel to rotate
        if brands and not hits:
            continue  # focus-brand mode: skip screens without the brand's ad
        found |= hits
        saved += 1
        name = f"{task['id']}_{saved}.jpg"
        await page.screenshot(path=str(jdir / name), type="jpeg", quality=82)
        task["images"].append(f"/shots/{jdir.name}/{name}")

    task["ads"] = total
    task["brands"] = brands
    task["brand_found"] = sorted(found)
    if brands and not OCR_STATE["ok"]:
        task["note"] = (task.get("note", "") + " | banner-image reading (OCR) unavailable on this server").strip(" |")
    if brands and not saved:  # keep one top-of-page shot as proof the brand ad was not there
        await page.evaluate(TOP_JS)
        await page.wait_for_timeout(800)
        name = f"{task['id']}_brand-not-found.jpg"
        await page.screenshot(path=str(jdir / name), type="jpeg", quality=82)
        task["images"].append(f"/shots/{jdir.name}/{name}")


async def run_task(job: dict, task: dict, loc: dict):
    async with SEM:
        task["status"] = "running"
        ctx = None
        try:
            browser = await get_browser()
            args = dict(_pw.devices[DEVICES[job["device"]]])
            args.update(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            )
            if loc["lat"] is not None:
                args.update(
                    geolocation={"latitude": loc["lat"], "longitude": loc["lng"]},
                    permissions=["geolocation"],
                )
            ctx = await browser.new_context(**args)
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await ctx.new_page()
            page.set_default_timeout(20000)
            flow = amazon_flow if task["platform"] == "amazon" else flipkart_flow
            task["note"] = await flow(page, task["keyword"], loc)
            await capture(page, SHOTS / job["id"], task, job["highlight"], job["screens"], job["brands"])
            task["status"] = "done"
        except Exception as e:  # noqa: BLE001
            task["status"] = "error"
            task["error"] = str(e).split("\n")[0][:220]
        finally:
            if ctx:
                await ctx.close()


async def run_job(job: dict, locs: dict):
    await asyncio.gather(*(run_task(job, t, locs[t["location"]]) for t in job["tasks"]))
    job["finished"] = True


# -------------------------------------------------------------------- API ---
@app.post("/api/jobs")
async def create_job(req: JobRequest):
    keywords = list(dict.fromkeys(k.strip() for k in req.keywords if k.strip()))
    platforms = [p for p in dict.fromkeys(req.platforms) if p in ("amazon", "flipkart")]
    if not keywords or not req.locations or not platforms:
        raise HTTPException(400, "Add at least one keyword, one city or pincode, and one platform.")
    if req.device not in DEVICES:
        raise HTTPException(400, "Unknown device.")
    screens = max(1, min(10, req.screens))
    brands = list(dict.fromkeys(b.strip() for b in req.brands if b.strip()))[:5]

    locs = {}
    try:
        for raw in req.locations:
            loc = resolve_location(raw)
            locs[loc["label"]] = loc
    except ValueError as e:
        raise HTTPException(400, str(e))

    total = len(keywords) * len(locs) * len(platforms)
    if total > MAX_TASKS_PER_JOB:
        raise HTTPException(
            400, f"{total} screenshots requested; the limit is {MAX_TASKS_PER_JOB} per run. Split it up."
        )
    if sum(1 for j in JOBS.values() if not j["finished"]) >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, "The server is busy with other runs. Try again in a minute.")

    jid = uuid.uuid4().hex[:10]
    (SHOTS / jid).mkdir(parents=True, exist_ok=True)
    tasks = []
    for kw in keywords:
        for label in locs:
            for p in platforms:
                tasks.append({
                    "id": f"{slug(p)}_{slug(kw)}_{slug(label)}",
                    "platform": p, "keyword": kw, "location": label,
                    "pincode": locs[label]["pincode"],
                    "status": "queued", "images": [], "ads": 0, "note": "", "error": "",
                    "brands": brands, "brand_found": [],
                })
    job = {"id": jid, "created": time.time(), "finished": False, "device": req.device,
           "screens": screens, "highlight": req.highlight, "brands": brands, "tasks": tasks}
    JOBS[jid] = job
    asyncio.create_task(run_job(job, locs))
    return {"job_id": jid, "total": total}


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Run not found (it may have expired).")
    return {k: job[k] for k in ("id", "finished", "tasks")}


@app.get("/api/jobs/{jid}/zip")
async def download_zip(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Run not found.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((SHOTS / jid).glob("*.jpg")):
            z.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="adshot_{jid}.zip"'},
    )


@app.get("/api/cities")
async def cities():
    return {"cities": list(CITIES)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/shots", StaticFiles(directory=SHOTS), name="shots")
