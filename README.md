# AdShot

Mobile-view screenshots of Amazon.in and Flipkart search pages for many keywords x many
Indian cities / pincodes, with "Sponsored" / "Ad" placements outlined in red.

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render -> New -> Blueprint (reads `render.yaml`) or New -> Web Service -> Docker.
3. Open the `https://<name>.onrender.com` link. Share it.

## Run locally
    pip install -r requirements.txt && playwright install chromium
    uvicorn app:app --reload

## Env vars
- `CONCURRENCY` parallel pages (default 2)
- `MAX_TASKS_PER_JOB` keyword x city x platform cap per run (default 24)
- `PROXY_URL` optional proxy (an Indian residential/mobile proxy greatly reduces CAPTCHAs)
- `KEEP_HOURS` how long screenshots are kept (default 3)

## Known limits
- Render's datacenter IPs are often challenged by Amazon/Flipkart. Without a proxy expect some "Blocked" results.
- Pincode entry uses the sites' own UI; if they change their markup, update `amazon_flow` / `flipkart_flow` in `app.py`.
- Sponsored detection is text-label based (best effort). Always eyeball the screenshots.
- Screenshots are stored on temporary disk and deleted after `KEEP_HOURS`.
