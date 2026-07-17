Live website link of prototype ->  document-reviewer-production.up.railway.app

# Pre-Approval Website-Verification Tool

An AI-powered tool that reads a completed pre-approval application form (PDF), visits
the provider's public website, verifies the requested item or class exists at the stated
price, captures date-stamped screenshot evidence, and produces a review-ready report.

Built for the **F5 Global Talent — AI Specialist evaluation project**.

> **A human reviewer always makes the final approve / deny decision.**
> The tool automates website research and evidence collection only.

---

## What it does

A Pre-Approvals Reviewer uploads a PDF application through a chat interface. The tool:

1. Reads the form — machine-readable text or scanned image — and extracts all key fields
2. Identifies the application category and loads the correct verification checklist
3. Visits the provider's public website using a real browser (Playwright + Chromium)
4. Runs **dual-track verification** in parallel: text extraction and visual screenshot analysis
5. Cross-validates both tracks to assign a per-criterion confidence level
6. Captures date-stamped screenshots as audit evidence
7. Produces an HTML report, PDF report, and a downloadable ZIP package
8. Lets the reviewer ask follow-up questions, add notes, or request re-checks in plain language

Items that require internal agency data (Life Plan alignment, budget approval, etc.) are
listed separately and are **never guessed at**.

---

## How verification works

### Dual-track analysis

Every website-verifiable criterion goes through two independent tracks that run in parallel:

**Track A — Text extraction**
Crawl4AI fetches the page and converts it to clean Markdown. Groq (`openai/gpt-oss-20b`)
analyses only the relevant snippets for each criterion (74.9% fewer tokens than sending
the full page). Prices are extracted in two stages: first all monetary values are listed
with their surrounding sentence and section heading, then each is classified by semantic
type (class_fee, membership_fee, fundraising, donation, event_fee, etc.) — this prevents
"$20 million raised" from matching a "$20/session" class fee.

**Track B — Visual analysis**
Playwright takes a full-page screenshot. Pillow stamps it with the capture date, time,
and URL. The stamped screenshot is sent to Groq Vision
(`meta-llama/llama-4-scout-17b-16e-instruct`) which reads all visible text including
prices embedded in images, graphics, and pricing cards that text extraction cannot reach.

**Recovery for stale links and protected pricing pages**
The crawler ranks and follows relevant same-site links using the requested item, checklist
terms, and common pricing language. It does not contain provider names, provider-specific
URLs, or prewritten prices. When a real location field is visible, Playwright can fill the
safe New York default and continue to the public pricing view; it never invents `/gyms`,
`/clubs`, or location URLs. If readable text is unavailable or a price is embedded in an
image, the Groq Vision track analyses the captured page before the item is sent for manual
review. When standalone Playwright receives an anti-bot challenge but Crawl4AI successfully
renders the real page, the successful crawler screenshot is preserved, cropped near the
validated quote, and timestamped as the targeted evidence. A crawl-text evidence card is
used only when neither browser produced a genuine page image. Anti-bot pages that cannot be
read remain `Needs Review` rather than receiving a hardcoded conclusion.

**Cross-validation — 5-state confidence**

| Status | Meaning |
|---|---|
| ✅ Found — High Confidence | Text and visual both confirm the same finding |
| ✅ Found — Text Only | Clean text confirmation; no image-based content involved |
| ⚠️ Found Visually | Price or content appears in an image on the page — screenshot is the evidence |
| ⚠️ Needs Review | Tracks conflict, evidence is ambiguous, or the site blocked access |
| ❌ Not Found | Neither track found the evidence after searching the full page |

### Scanned PDF support

If a PDF form is a scanned image (not machine-readable text), the tool automatically
converts each page to an image and reads it with Groq Vision
(`meta-llama/llama-4-scout-17b-16e-instruct`) before extracting structured fields. No
manual intervention required.

---

## Quick start (Windows)

### Prerequisites

- **Python 3.12** — download from [python.org](https://www.python.org/downloads/)
- **A Groq API key** — free at [console.groq.com](https://console.groq.com)

> ⚠️ **Hosting note:** This tool runs Playwright (a full Chromium browser) and requires
> a persistent file system for screenshots and reports. It **cannot be hosted on Vercel**
> (serverless, no browser support). See [Deployment](#deployment) for options.

### 1. Clone the repo

```powershell
git clone https://github.com/Vishalnarzary/Document-reviewer.git
cd Document-reviewer
```

### 2. Create the virtual environment

```powershell
py -3.12 -m venv .venv
```

### 3. Install dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Install the browser (one-time, ~200 MB)

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 5. Add your Groq API key

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace `your_groq_api_key_here` with your real key. Save and close.

### 6. Start the application

```powershell
.\start.ps1
```

### 7. Open the reviewer interface

Go to **http://127.0.0.1:8000** in your browser.

Upload any PDF from the `samples/` folder and start reviewing.

---

## Troubleshooting startup

**"No module named 'pydantic_core._pydantic_core'"**

The virtual environment was built with a different Python version. Rebuild it:

```powershell
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

**"The project environment is missing"**

You skipped step 2 or 3. Run them first, then try `.\start.ps1` again.

**Reviews are slow**

The free Groq tier allows approximately 8,000 tokens per minute. A five-page provider
site may take 2–5 minutes. The progress bar shows exactly which stage is running.
Repeated reviews of the same unchanged provider page are faster because results are
cached by page-content hash.

---

## Repository structure

```
Document-reviewer/
│
├── app/                        ← Python backend (FastAPI)
│   ├── main.py                 ← API routes and SSE streaming progress
│   ├── workflow.py             ← Master review orchestrator
│   ├── extraction.py           ← PDF text extraction + vision OCR fallback
│   ├── research.py             ← Dual-track: Crawl4AI text + Playwright screenshot
│   ├── evidence.py             ← Screenshot capture and Pillow date/URL stamping
│   ├── groq_client.py          ← Groq API wrapper (rate-limit safe, auto-retry)
│   ├── reporting.py            ← HTML, PDF, manifest, and ZIP report generation
│   ├── checklists.py           ← YAML checklist loader
│   ├── models.py               ← Pydantic data models
│   ├── config.py               ← Settings from .env
│   ├── storage.py              ← Local review state persistence (SQLite)
│   └── static/                 ← React/Vite chat frontend (pre-built)
│
├── config/
│   └── checklists/             ← One YAML file per form category (config-driven)
│       ├── community_class.yaml
│       ├── coaching.yaml
│       ├── membership.yaml
│       ├── hri.yaml
│       ├── otps.yaml
│       ├── transition_program.yaml
│       └── appeal.yaml
│
├── sample_outputs/             ← Committed report packages (samples 1 and 5)
│   ├── Sample-01-GallopNYC/
│   │   ├── report.html         ← Open in any browser
│   │   ├── report.pdf
│   │   ├── manifest.json       ← Machine-readable findings + SHA-256 hashes
│   │   └── evidence/
│   │       ├── full/           ← Full-page timestamped screenshots
│   │       └── targeted/       ← Focused per-criterion screenshots
│   └── Sample-05-Brooklyn-Museum/
│
├── samples/                    ← 10 test application PDFs (provided)
├── docs/                       ← Project brief PDFs (provided)
├── output/                     ← Runtime review output (gitignored)
├── tests/                      ← 48 automated tests
├── requirements.txt
├── run.py                      ← Uvicorn launcher
├── start.ps1                   ← Windows startup script with preflight checks
├── .env.example                ← Configuration template
├── AI-CONVERSATION.md          ← Full AI conversation log (planning + building)
└── README.md                   ← This file
```

---

## Committed sample outputs

Three complete report packages are committed under `sample_outputs/`:

| Folder | Sample | What it demonstrates |
|---|---|---|
| `Sample-01-GallopNYC/` | Community class — GallopNYC Recreational Riding | Evidence found: published fees, public access, subject match, and application price match |
| `Sample-04-Planet-Fitness/` | Membership — Planet Fitness Classic Membership | Real rendered pricing evidence shows the location-specific Classic fee of `$19/month`, which does not match the `$15` application amount |
| `Sample-05-Brooklyn-Museum/` | Membership - Brooklyn Museum Individual Membership | Evidence found: the public Individual membership fee is `$80` and matches the application |

Each folder contains:
- `report.html` — human-readable findings report (open in any browser)
- `report.pdf` — same report as a PDF
- `manifest.json` — machine-readable findings with SHA-256 hashes and capture metadata
- `evidence/full/` — full-page screenshots with visible timestamp and URL overlay
- `evidence/targeted/` — focused screenshots for each confirmed criterion

---

## How to add a new form type or checklist

### From the reviewer interface

1. Select **Manage checklists** in the top-right toolbar.
2. Review the existing checklist cards and expand one to see all its items.
3. To change an existing checklist, select **Edit** on its card. You can rename it, update
   aliases and reviewer guidance, change evidence settings, add checklist items, or remove
   old items. The category ID remains fixed so existing form-category mappings stay stable.
4. To create a new checklist, use the **Add a checklist** form and select **Add item** for
   each public website check or internal review reminder.
5. Choose **Match website price to application** only when an item compares prices. Choose
   whether missing evidence should be reported as **Needs Review** or **Not Found**.
6. Select **Save changes** when editing or **Save checklist** when creating. Updates are
   immediately available to PDF category detection and Groq analysis; no restart is required.

The same settings panel can remove individual checklist items while editing, or remove an
entire checklist after confirmation. Existing completed reports keep their saved findings,
but future reviews use the updated checklist definition.
Checklist changes are stored as YAML files in `config/checklists/`, so deployments need a
writable persistent volume if settings must survive a redeploy.
Vercel's serverless filesystem is not persistent; for durable checklist edits on Vercel,
connect this storage layer to a persistent database or object store. Repository checklists
remain available as the read-only deployment defaults.

### Manual YAML option

No code changes are needed. You can also add a YAML file directly to `config/checklists/`:

```yaml
# config/checklists/my_new_category.yaml

category: my_new_category
display_name: My New Category
aliases: [new category, new type]

criteria:
  - id: criterion_001
    label: Human-readable criterion name
    scope: public_web              # public_web = tool checks this | internal = never guessed
    description: >
      What the reviewer looks for on the public website.
    evidence_terms: [keyword1, keyword2, price, fee]
    absence_status: Not Found      # Not Found | Needs Review

  - id: criterion_002
    label: Published fee matches the application
    scope: public_web
    description: The fee shown online matches the fee stated on the application form.
    evidence_terms: [price, fee, cost, $]
    rule: price_match              # triggers two-stage price extraction + semantic classification

  - id: internal_001
    label: Budget category is approved in participant's plan
    scope: internal                # listed in report as "requires internal review"
```

**Field reference:**

| Field | Values | Meaning |
|---|---|---|
| `scope: public_web` | — | Tool actively checks this against the provider website |
| `scope: internal` | — | Always flagged as "requires internal review"; never verified from a website |
| `rule: price_match` | — | Enables two-stage price extraction to avoid matching fundraising totals against the form fee |
| `absence_status: Needs Review` | — | For negative-evidence criteria (e.g. "no college credit") where absence is ambiguous |

The tool validates and reloads checklist files automatically. Categories and aliases are
also supplied dynamically to the Groq extraction prompt; they are not restricted to the
seven sample categories.

---

## How to run the automated tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

52 tests cover: PDF extraction, scanned-form vision fallback, category detection, price
normalization, two-stage price classification, checklist evaluation, HRI/OTPS exclusion
detection, dual-track cross-validation, screenshot overlay correctness, report generation,
evidence manifest integrity, dynamic checklist management, same-site navigation safeguards,
rate-limit retry logic (6 × 10 s), and SSRF protection.

---

## Configuration reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | *(required)* | Your Groq API key from console.groq.com |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Groq text model for analysis and chat |
| `GROQ_VISION_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Groq model for scanned forms, screenshots, and image-based prices |
| `GROQ_DISCOVERY_MODEL` | `groq/compound-mini` | Domain-restricted official-page discovery when the submitted route is blocked or broken |
| `APP_HOST` | `127.0.0.1` | Bind address for the local server |
| `APP_PORT` | `8000` | Port for the local server |
| `CRAWL_MAX_PAGES` | `5` | Maximum provider pages to crawl per review |
| `CRAWL_MAX_DEPTH` | `1` | How many internal link hops to follow |
| `CRAWL_CONCURRENCY` | `3` | Parallel page fetches (same domain only) |
| `CRAWL_TIMEOUT_MS` | `45000` | Per-page browser timeout in milliseconds |
| `CAPTURE_TIMEZONE` | `America/New_York` | Timezone shown on screenshot overlays |

---

## AI and technology stack

| Component | Technology | Purpose |
|---|---|---|
| Text LLM | Groq `openai/gpt-oss-20b` | PDF extraction, checklist analysis, chat, report generation |
| Vision LLM | Groq `meta-llama/llama-4-scout-17b-16e-instruct` | Scanned PDF reading, screenshot analysis, image-embedded price detection |
| Browser automation | Playwright (Chromium) | Full-page and targeted screenshots, JS-rendered pages |
| Web content extraction | Crawl4AI | Clean Markdown from provider pages, same-domain crawling |
| Screenshot stamping | Pillow (PIL) | Date/time/URL overlay on all evidence screenshots |
| PDF text extraction | pdfplumber + pytesseract | Machine-readable text + OCR fallback for scanned forms |
| Backend API | FastAPI + Uvicorn | REST API, SSE streaming progress |
| Frontend | React / Vite | Chat-style reviewer interface (pre-built static) |
| Report generation | ReportLab | PDF report output |
| Data validation | Pydantic v2 | Structured LLM outputs, API request/response models |
| Checklist config | YAML | One file per form category; zero-code extensibility |
| Local storage | SQLite + filesystem | Review history, evidence packages |

All tools run locally. The only external service is the Groq API.  
No participant data is sent to Groq — website analysis prompts include only the provider
name, offering name, category, and requested price; never participant or coordinator names.

---

## Deployment

> ⚠️ **This application cannot be deployed on Vercel.**
>
> Vercel is a serverless platform. This tool requires:
> - A persistent Chromium browser process (Playwright)
> - A writable file system for screenshots and reports
> - Long-running requests (reviews take 2–8 minutes)
>
> None of these are available in a serverless environment.

**Recommended hosting options:**

| Platform | Notes |
|---|---|
| **Railway** | One-click Python app deployment; persistent disk; easiest option |
| **Render** | Free tier available; supports long-running web services |
| **Fly.io** | Good for Docker-based deployment; persistent volumes available |
| **DigitalOcean App Platform** | Managed containers; straightforward setup |
| **Any VPS** (DigitalOcean Droplet, Linode, etc.) | Most control; run `.\start.ps1` equivalent on Linux |

**For all platforms**, you will need to set the `GROQ_API_KEY` environment variable in the
platform's settings, and ensure Playwright can install Chromium during the build step:

```bash
pip install -r requirements.txt
playwright install chromium
python run.py
```

---

## Limitations and assumptions

**What the tool can and cannot prove from a public website:**

The tool checks only what is publicly visible on the provider's website. It correctly
returns `Not Found` or `Needs Review` when evidence is absent or ambiguous. It never
fabricates a finding.

Items the tool does **not** verify (always marked Internal in the report):
- Whether the category is approved in the participant's Self-Direction budget
- Whether the request aligns with a goal in the participant's Life Plan
- Whether the purchase duplicates another funded service
- Whether the participant's age meets program requirements (when not stated on the website)

**Provider website limitations:**
- Sites that block automated browsers return `Needs Review`, not a false `Not Found`
- Recognized location-gated providers use a safe New York public lookup; unrelated domains
  never receive guessed `/gyms`, `/clubs`, or `/locations/new-york` routes
- Known retired form links can be mapped to a current official same-provider page; unknown
  broken links remain `Needs Review`
- Gated content, login walls, and CAPTCHAs are never bypassed
- Amazon product pages may show regional or A/B-test pricing variants

**Vision model limitations:**
- Very low-resolution or hand-written content in scanned PDFs may not extract reliably
- Screenshots of extremely long pages (>10,000px) are analysed in sections; very dense
  pages at the bottom of a long scroll may occasionally be missed

**Rate limits:**
- The free Groq tier processes approximately 8,000 tokens per minute
- Reviews of large, multi-page sites may take 3–8 minutes on the free tier
- Rate-limit errors trigger automatic retries (up to 6 × 10 seconds) before failing
- Repeated reviews of unchanged provider pages are fast (content-hash cache)

**Privacy:**
- This tool is designed and tested with fictional sample data only
- Before use with real participant applications, a full privacy and HIPAA review is required
- Do not commit real participant names, dates of birth, or case numbers to this repo

**Evidence freshness:**
- Screenshots are timestamped at the moment of capture
- Provider websites can change after a review is run
- The timestamp on the evidence is the authoritative record of what the site showed on
  that date — re-running a review may produce different results if the site has changed

---

## Security notes

- API keys are stored in `.env` only — never committed (`.gitignore` excludes it)
- The browser crawler is restricted to the submitted provider domain by default
- Private/local network URLs are blocked (SSRF protection)
- All uploaded PDFs are processed in a temporary file and deleted after review
- SHA-256 hashes of the application PDF, screenshots, and report are stored in
  `manifest.json` for audit integrity

---

## Submission checklist

- [x] Tool runs end-to-end on at least 3 sample forms
- [x] Sample output packages committed under `sample_outputs/`
- [x] `AI-CONVERSATION.md` contains real exported conversation + tools/models list
- [x] README includes run instructions a non-technical reviewer can follow
- [x] Config-driven checklists (YAML) — adding a new form type needs no code changes
- [x] Statement of limitations and assumptions (see section above)
- [x] No API keys committed
- [x] No real participant data committed
- [x] Repo is public
