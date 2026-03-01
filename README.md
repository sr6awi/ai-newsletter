# AI Weekly Newsletter Pipeline

Automated system that collects AI news from 12 RSS feeds, scores and summarizes articles with Google Gemini, and delivers a professional HTML newsletter via email.

**Two execution paths:**
- **n8n workflows** — visual, self-hosted, runs on Docker
- **Python script** — standalone fallback, runs via cron

All services use free tiers. No paid subscriptions required.

---

## Architecture

```
RSS Feeds (12 sources)
    │
    ▼
┌──────────────────┐     ┌──────────────────┐
│  n8n Collection   │ OR  │  Python Pipeline  │
│  (every 4 hours)  │     │  (cron / manual)  │
└────────┬─────────┘     └────────┬─────────┘
         │                         │
         ▼                         ▼
┌──────────────────┐     ┌──────────────────┐
│  Google Sheets    │     │  SQLite DB        │
│  (dedup store)    │     │  (dedup store)    │
└────────┬─────────┘     └────────┬─────────┘
         │                         │
         ▼                         ▼
┌──────────────────────────────────────────┐
│  Google Gemini 1.5 Flash API             │
│  • Relevance scoring (1-10)              │
│  • 2-sentence summaries                  │
│  • Category assignment                   │
└────────────────────┬─────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────┐
│  HTML Email Generation                   │
│  • Dark header, categorized sections     │
│  • Inline CSS, responsive design         │
└────────────────────┬─────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────┐
│  Outlook SMTP → Email Delivery           │
└──────────────────────────────────────────┘
```

## RSS Sources

| Source | Category | Feed |
|--------|----------|------|
| Anthropic Blog | Research | anthropic.com/feed.xml |
| OpenAI Blog | Research | openai.com/blog/rss.xml |
| Google DeepMind | Research | deepmind.google/blog/rss.xml |
| Hugging Face | Research | huggingface.co/blog/feed.xml |
| TechCrunch AI | Industry | techcrunch.com/.../feed/ |
| The Verge AI | Industry | theverge.com/.../index.xml |
| VentureBeat AI | Industry | venturebeat.com/.../feed/ |
| Ars Technica | Industry | feeds.arstechnica.com/... |
| MIT Tech Review | Research | technologyreview.com/feed/ |
| NVIDIA AI Blog | Product Launch | blogs.nvidia.com/feed/ |
| AWS AI Blog | Product Launch | aws.amazon.com/.../feed/ |
| IEEE Spectrum AI | Research | spectrum.ieee.org/.../rss |

---

## Quick Start

```bash
# 1. Clone / download the project
cd ai-newsletter

# 2. Run setup (installs Docker, n8n, Python deps)
chmod +x setup.sh
./setup.sh

# 3. Edit credentials
cp .env.example .env
nano .env   # fill in your API keys

# 4. Choose your path:
#    Option A: n8n workflows (see "n8n Setup" below)
#    Option B: Python pipeline
source venv/bin/activate
python newsletter_pipeline.py --dry-run
```

---

## Getting API Keys (All Free)

### 1. Google Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Sign in with your Google account
3. Click **"Create API key"**
4. Copy the key to `GEMINI_API_KEY` in your `.env`

Free tier: 15 requests/minute, 1 million tokens/day — more than enough.

### 2. Microsoft Outlook / Office 365 SMTP

No API key needed — configure SMTP credentials directly in n8n:

1. In n8n, go to **Settings > Credentials > Add Credential**
2. Select **SMTP**
3. Fill in:
   - **Host:** `smtp.office365.com`
   - **Port:** `587`
   - **Security:** `STARTTLS`
   - **User:** your Outlook email address (e.g. `newsletter@yourcompany.com`)
   - **Password:** your Outlook account password (or an [app password](https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9) if MFA is enabled)
4. Save the credential and name it **"Outlook SMTP"**
5. In your `.env`, set:
   - `OUTLOOK_FROM_EMAIL` — the sender address (must match your SMTP login)
   - `OUTLOOK_TO_EMAIL` — the corporate recipient or distribution list

### 3. Google Sheets + Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**:
   - APIs & Services > Library > search "Google Sheets API" > Enable
   - APIs & Services > Library > search "Google Drive API" > Enable
4. Create a service account:
   - APIs & Services > Credentials > Create Credentials > Service Account
   - Name it (e.g., "newsletter-bot"), click through
   - On the service account page, go to **Keys** > Add Key > Create new key > JSON
   - Download the JSON file, save as `service_account.json` in the project root
5. Create a Google Sheet with **3 tabs** (one spreadsheet, three sheets):

   **Tab 1 — `Articles`** (RSS queue + dedup store):
   ```
   hash | title | url | date | source | content_snippet | category | processed | collected_at
   ```

   **Tab 2 — `Processed`** (newsletter archive — one row per selected article per send):
   ```
   title | url | source | summary | relevance_score | category | newsletter_date | date_range
   ```

   **Tab 3 — `Newsletter_Log`** (send history — one row per weekly send):
   ```
   send_date | date_range | articles_selected | articles_analyzed | subject | status
   ```

   - Copy the spreadsheet ID from the URL (the long string between `/d/` and `/edit`)
   - Set `GOOGLE_SHEETS_ID` in your `.env`
6. Share the spreadsheet with the service account email (found in the JSON file, looks like `name@project.iam.gserviceaccount.com`) — give **Editor** access

---

## n8n Setup

### Install & Start

The setup script handles this automatically, but manually:

```bash
# Start n8n with Docker
docker compose up -d

# Access at http://localhost:5678
```

### Import Workflows

1. Open n8n at `http://localhost:5678`
2. Complete the initial setup wizard
3. For each workflow file:
   - Click the **"..."** menu (top-right) > **Import from File**
   - Select `n8n_workflow_collection.json` — RSS collection (every 4 hours)
   - Repeat for `n8n_workflow_newsletter.json` — newsletter generation (Monday 9 AM)

### Configure Credentials in n8n

**Google Sheets:**
1. Go to Settings > Credentials > Add Credential
2. Select **Google Sheets API (OAuth2)** or **Google Sheets API (Service Account)**
3. For Service Account: paste the contents of your `service_account.json`
4. Save, then update both workflow nodes to use this credential

**Environment Variables:**
The `docker-compose.yml` passes your `.env` values into n8n. Workflows reference them via `$env.GEMINI_API_KEY`, `$env.BREVO_API_KEY`, etc.

### Activate Workflows

1. Open each imported workflow
2. Update the Google Sheets nodes with your actual spreadsheet URL
3. Toggle the workflow to **Active** (top-right switch)
4. The collection workflow runs every 4 hours; the newsletter runs Monday 9 AM UTC

---

## Python Pipeline

The Python script is a standalone alternative that does the same thing without n8n.

### Setup

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate    # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### Usage

```bash
# Full pipeline: collect + score + generate + send
python newsletter_pipeline.py

# Collect RSS feeds only (populate the database)
python newsletter_pipeline.py --collect-only

# Generate and send newsletter only (from existing data)
python newsletter_pipeline.py --send-only

# Generate HTML but don't send email (preview in output/)
python newsletter_pipeline.py --dry-run
```

### Storage Options

The Python pipeline supports two backends:

- **SQLite** (default): No external setup needed. Data stored in `newsletter.db`
- **Google Sheets**: Set `STORAGE_BACKEND=sheets` in `.env`. Requires service account setup (see above)

### Cron Setup

Run the full pipeline every Monday at 9 AM:

```bash
crontab -e
# Add:
0 9 * * 1 cd /path/to/ai-newsletter && ./venv/bin/python newsletter_pipeline.py >> logs/pipeline.log 2>&1
```

For more frequent collection (matches n8n's 4-hour schedule):

```bash
# Collect every 4 hours
0 */4 * * * cd /path/to/ai-newsletter && ./venv/bin/python newsletter_pipeline.py --collect-only >> logs/collect.log 2>&1

# Send newsletter Monday 9 AM
0 9 * * 1 cd /path/to/ai-newsletter && ./venv/bin/python newsletter_pipeline.py --send-only >> logs/send.log 2>&1
```

---

## Project Structure

```
ai-newsletter/
├── .env.example                    # Credential template
├── .env                            # Your credentials (gitignored)
├── docker-compose.yml              # Generated by setup.sh
├── n8n_workflow_collection.json    # n8n: RSS collection (every 4 hours)
├── n8n_workflow_newsletter.json    # n8n: Newsletter generation (Monday 9 AM)
├── newsletter_pipeline.py          # Python fallback pipeline
├── requirements.txt                # Python dependencies
├── setup.sh                        # Installation script
├── service_account.json            # Google service account key (gitignored)
├── newsletter.db                   # SQLite database (auto-created)
├── logs/                           # Pipeline logs
│   └── pipeline.log
├── output/                         # Generated HTML newsletters
│   └── newsletter_20260223.html
└── README.md                       # This file
```

## Email Preview

The generated newsletter features:
- Dark header (`#1a1a2e`) with title and date range
- Articles grouped by category with colored borders:
  - **Research** — green (#4CAF50)
  - **Product Launch** — blue (#2196F3)
  - **Industry** — orange (#FF9800)
  - **Policy** — purple (#9C27B0)
- Each article shows: linked title, 2-sentence summary, source badge, relevance score, date
- Responsive design (max-width 700px), works in all major email clients
- Footer with unsubscribe/preferences placeholders

---

## Troubleshooting

**n8n won't start:**
- Check Docker is running: `docker ps`
- Check logs: `docker compose logs n8n`
- Verify port 5678 isn't in use: `lsof -i :5678`

**RSS feeds returning empty:**
- Some feeds may block automated requests. The workflows use `continueOnFail` to skip broken feeds.
- Test a feed manually: `curl -s "https://openai.com/blog/rss.xml" | head -20`

**Gemini API errors:**
- Verify your API key: `curl "https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY"`
- Free tier limit: 15 requests/minute. The pipeline has built-in rate limiting.

**Outlook email not sending:**
- Verify the SMTP credential in n8n: host `smtp.office365.com`, port `587`, STARTTLS
- If MFA is enabled on the account, use an [app password](https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9) instead of your regular password
- Confirm `OUTLOOK_FROM_EMAIL` matches the authenticated SMTP account exactly
- Check your IT/admin hasn't blocked SMTP AUTH — some Office 365 tenants disable it by default (ask IT to enable it for the sending account)

**Google Sheets permission denied:**
- Make sure the service account email has Editor access to the spreadsheet
- Check that both Sheets API and Drive API are enabled in your GCP project

---

## License

MIT
