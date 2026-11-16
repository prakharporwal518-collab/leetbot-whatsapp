# 🤖 LeetBot — LeetCode solutions on WhatsApp

A WhatsApp AI chatbot that solves **any LeetCode problem in C++, C and Python** and explains
**pattern-printing problems** (stars, pyramids, diamonds…). Send a problem number, name or link
and get the idea, approach, complexity, a dry run and submittable code.

**Runs 100% free** — no paid services and no credit card anywhere.

```
You:      1
LeetBot:  *1. Two Sum (Easy)*
          *Idea:* store each number's index in a hash map ...
          *Approach:* 1. ... 2. ...
          *Complexity:* Time O(n), Space O(n)
          *Dry run:* nums = [2,7,11,15], target = 9 ...
          *C++*  ...code...
          *C*  ...code...
          *Python*  ...code...
```

## Features

- **Any LeetCode problem** — send a number (`1`, `#206`), a slug (`two-sum`) or a URL.
  The real statement and the exact function signatures are fetched from LeetCode, so the
  code can be pasted and submitted directly.
- **Three languages by default** — C++, C and Python. C code handles memory the LeetCode way
  (`malloc`, `returnSize`).
- **Pattern printing** — shows the expected output, explains what `i` and `j` control and how
  many spaces/stars go in each row, then gives full programs with `main()`.
- **Follow-ups** — remembers your last 6 messages: _"explain step 2"_, _"optimize it"_.
- **Saves messages** — packs the whole answer into 1–3 WhatsApp messages and never splits
  a code block unless it's too big for one message.
- **Survives free-tier limits** — retries on rate limits, then switches to a fallback model.

| You send | What happens |
|---|---|
| `hi` / `help` | One short help menu |
| `1`, `#206`, `two-sum`, a LeetCode URL | Solves that problem |
| anything else | Treated as a question, e.g. _print a hollow diamond of stars_ |
| `lang cpp` / `lang c` / `lang python` | Only that language from now on (shorter replies) |
| `lang all` | Back to all three (default) |

## Tech stack

| Part | Tool | Cost |
|---|---|---|
| Web server | Python, Flask, gunicorn | free |
| WhatsApp | Meta WhatsApp Cloud API (official, test number) | free |
| AI | Groq API via the `openai` package (switchable to Gemini) | free tier |
| Problems | LeetCode public GraphQL API | free |
| Hosting | Render free tier (+ cron-job.org keep-alive) | free |
| Local tunnel | ngrok | free |

## How it works

```
WhatsApp ──► Meta ──POST /whatsapp──► app.py ──► returns 200 at once
                                        │ (background thread)
                                        ├─► leetcode.py  fetch statement + signatures
                                        ├─► llm.py       ask Groq, parse JSON, retry on 429
                                        └─► whatsapp.py  pack into few messages, send
```

| File | Job |
|---|---|
| `app.py` | Flask routes, duplicate-message check, per-user memory, reply logic |
| `whatsapp.py` | Webhook verification, parsing, message packer/splitter, sending, monthly counter |
| `leetcode.py` | Number/slug/URL detection, number→slug cache, GraphQL fetch, HTML stripping |
| `llm.py` | Prompts, LLM call with retry + fallback model, JSON parsing |
| `tests/` | Unit tests (no real network calls) |

---

## Setup (step by step)

You need to fill in **4 keys**: `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`, `LLM_API_KEY`.
Meta's dashboard changes its layout now and then, so a button may sit in a slightly
different place than described here.

### 1. Create a Meta developer app

1. Go to <https://developers.facebook.com> → log in with Facebook → **My Apps** → **Create App**.
2. Choose use case **Other** → app type **Business** → give it a name → **Create**.
3. On the app dashboard find **WhatsApp** → **Set up**. Meta creates a free test
   WhatsApp Business Account for you.

### 2. Use the free test number

1. Open **WhatsApp → API Setup**.
2. Under **From** you'll see a *test number*. Copy its **Phone number ID** → this is `PHONE_NUMBER_ID`
   (it is a long ID, not the phone number itself).
3. Under **To** → **Manage phone number list** → add your own WhatsApp number and enter the code
   Meta sends you. The test number can only message numbers on this list (up to 5).

### 3. Create a permanent token (System User)

The token shown on the API Setup page expires in 24 hours. Make a permanent one:

1. Go to <https://business.facebook.com/settings> → **Users → System users** → **Add** →
   name it (e.g. `leetbot`) → role **Admin**.
2. Select the system user → **Assign assets**:
   - **Apps** → your app → *Full control*
   - **WhatsApp accounts** → your test account → *Full control*
3. Click **Generate token** → choose your app → expiration **Never** → tick
   `whatsapp_business_messaging` and `whatsapp_business_management` → **Generate**.
4. Copy the token → this is `WHATSAPP_TOKEN`. You can't see it again later, so save it now.

### 4. Get a free Groq API key

1. Go to <https://console.groq.com> → sign up (no card needed) → **API Keys** → **Create API Key**.
2. Copy it (starts with `gsk_`) → this is `LLM_API_KEY`.

> Model names change over time. If you see a "model not found" error in the logs, pick current
> ones from <https://console.groq.com/docs/models> and update `LLM_MODEL` / `LLM_FALLBACK_MODEL`.

### 5. Run locally with ngrok

```bash
git clone https://github.com/prakharporwal518-collab/leetbot-whatsapp.git
cd leetbot-whatsapp
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Open `.env` and fill in the 4 keys. `VERIFY_TOKEN` is any secret word you invent
(e.g. `leetbot-secret-42`). Then start the bot:

```bash
python app.py
```

In a **second terminal**, open a public tunnel to it (free account at <https://ngrok.com>,
then run `ngrok config add-authtoken <your-token>` once):

```bash
ngrok http 5000
```

ngrok prints a URL like `https://abcd-1234.ngrok-free.app`. Now connect Meta to it:

1. Meta app → **WhatsApp → Configuration** → **Webhook → Edit**.
2. **Callback URL**: `https://abcd-1234.ngrok-free.app/whatsapp`
3. **Verify token**: the same word you put in `VERIFY_TOKEN` → **Verify and save**.
4. Under **Webhook fields**, click **Subscribe** next to **messages**.

Send `hi` to the test number on WhatsApp — the bot should reply. 🎉

### 6. Deploy free on Render

1. Push this repo to your GitHub.
2. <https://render.com> → sign up with GitHub (no card) → **New → Blueprint** → pick this repo.
   Render reads `render.yaml` and asks you for the 4 secret keys.
   (Manual alternative: **New → Web Service**, instance type **Free**,
   build `pip install -r requirements.txt`, start
   `gunicorn app:app --workers 1 --threads 8 --timeout 120`, then add all 7 variables from
   `.env.example` under **Environment**.)
3. When it's live you get a URL like `https://leetbot-whatsapp.onrender.com`.
   Open `/health` in a browser — it should show `ok`.
4. Back in Meta → **WhatsApp → Configuration** → change the Callback URL to
   `https://leetbot-whatsapp.onrender.com/whatsapp` (same verify token) → **Verify and save**.

> Why `--workers 1`? The bot keeps its memory (seen message IDs, chat history, language choice)
> inside the process. With several workers each would have its own separate memory.

### 7. Keep it awake with cron-job.org

Render's free tier goes to sleep after 15 minutes without traffic, and waking up takes
about a minute.

1. <https://cron-job.org> → free account → **Create cronjob**.
2. URL: `https://leetbot-whatsapp.onrender.com/health` — schedule: **every 10 minutes**.

If the bot does fall asleep, Meta re-sends the message; LeetBot remembers the last 500
message IDs so you never get a duplicate answer.

### 8. "Stay free" checklist

- [ ] **Never add a card** on Meta, Groq or Render. Without a card, nothing can be charged.
- [ ] Stay on the Meta **test number** (max 5 recipients — fine for personal use).
- [ ] Budget for **1,000 free WhatsApp messages per month**. The bot logs
      `Messages sent this month: N / 1000` after every message (the counter is in memory,
      so it restarts from 0 when the server restarts). Meta has changed its pricing rules
      several times — check the current ones at
      <https://developers.facebook.com/docs/whatsapp/pricing>.
- [ ] Use `lang cpp` (or `c` / `python`) to get one language — shorter replies, fewer messages.
- [ ] Groq free tier has per-minute and per-day limits. If you hit them the bot replies
      "Too many requests, try again in a minute."
- [ ] Keep `.env` out of GitHub (it's already in `.gitignore`).

## Switching to Gemini's free tier

Get a key at <https://aistudio.google.com> and change these lines in `.env`
(or in Render's Environment tab):

```
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-2.0-flash
LLM_FALLBACK_MODEL=gemini-2.0-flash-lite
LLM_API_KEY=your-gemini-key
```

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

The tests replace LeetCode, the LLM and WhatsApp with fakes, so they need no keys and
send no messages.

## Troubleshooting

| Problem | Likely cause |
|---|---|
| Webhook verification fails | `VERIFY_TOKEN` differs from the one typed in Meta, or the URL doesn't end in `/whatsapp` |
| Bot receives but never replies | `WHATSAPP_TOKEN` expired (use the permanent one), wrong `PHONE_NUMBER_ID`, or your number isn't in the recipient list — look for `WhatsApp API error` in the logs |
| No webhook arrives at all | You didn't subscribe to the **messages** field, or ngrok's URL changed after a restart |
| Answer isn't the exact LeetCode signature | LeetCode blocked the request from the server (see `LeetCode fetch failed` in logs); the bot then answers from the LLM's memory |
| "Too many requests" often | Groq free-tier limit — wait a minute, or use `lang cpp` for smaller requests |

## License

MIT — see [LICENSE](LICENSE).
