# Hosting the BD Signals app (Railway)

One service runs everything: the web UI behind a password, the Operate page (run enrich / scan / score / dry runs),
and the Monday 05:00 America/Chicago weekly run. They share one SQLite database on a volume, so what you curate in the
UI is exactly what the weekly run uses.

## Environment variables (set these in the Railway dashboard, never in chat or in git)

| Variable | Required | What it is |
|---|---|---|
| `BD_UI_PASSWORD` | yes | The sign-in password. 16+ characters or the app refuses to start. Changing it signs everyone out. |
| `ANTHROPIC_API_KEY` | for enrich/scan/brief | Your Anthropic API key. |
| `ANTHROPIC_MODEL` | for enrich/scan/brief | Model id to use. Not hard-coded anywhere. |
| `BD_USER_AGENT` | for the edgar scanner | Company name and contact email (SEC fair access), e.g. `SEI Chicago BD (you@sei.com)`. |
| `TEAMS_WEBHOOK_URL` | for posting | Without it the Monday run builds the card but posts nothing. |
| `BD_DRY_RUN` | optional | `1` = the Monday run never posts, even if the webhook is set. |
| `BD_DATA_DIR` | set by the image | `/data`. Mount the volume here. |
| `PORT` | set by Railway | Port the app listens on. |

## Steps

1. Create a Railway project with a service from this repo (Dockerfile build; `railway.json` sets the healthcheck).
2. Add a **volume mounted at `/data`** (the database, job logs and proposal files live there; without it everything is lost on redeploy).
3. Add the variables above. Start with only `BD_UI_PASSWORD` if you just want to look around.
4. Generate a public domain for the service and open it. Sign in with the password.

## First use

* On first boot the 58 target accounts load from `accounts.csv`. Their facts are blank, so every account is **held** by the fit gate.
* **Operate → Discover** (start with 2 segments) finds new Chicagoland companies; those that pass the fit gate appear on Accounts as *auto-added*, the rest wait on the **Discovery** page. Mark auto-added accounts reviewed once you have looked at them.
* **Operate → Enrich** proposes missing facts (revenue, employees, ownership, HQ) from public sources. **Enrich review** shows each proposal with its source; you accept the ones you trust. Nothing is applied automatically.
* **Operate → Scan** (start with 5 accounts to see the cost), then **Score**, then **Build card (dry run)**. Leads appear on the **Leads** page; cost per run on **Runs & cost**.
* Monday 05:00 Chicago the service runs scan, score and card by itself. It posts only if `TEAMS_WEBHOOK_URL` is set and `BD_DRY_RUN` is not `1`. There is no button anywhere that posts.

## Operating notes

* Back up `/data/bd.sqlite` (the **Accounts** and **Exclusions** pages are the master copy; the CSVs in the image are only the first-boot seed).
* One job runs at a time; a job is stopped after 2 hours. If the service restarts mid-run the job shows as "interrupted" and the Monday catch-up (up to 12 hours late) starts it again if it never ran that week.
* A single replica only. Do not scale this service to more than one instance (SQLite, in-memory login throttle, scheduler).
* This is a public URL protected by one shared password. See SECURITY.md section 4b; ask IT about putting it behind SSO (Cloudflare Access, Tailscale, or SEI-managed hosting) before real use with confidential target lists.
