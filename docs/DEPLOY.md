# Hosting the BD Signals app (Railway)

One service runs everything: the web UI behind a password, the Operate page (run enrich / scan / score / dry runs),
and the Monday 05:00 America/Chicago weekly run. They share one SQLite database on a volume, so what you curate in the
UI is exactly what the weekly run uses.

## Environment variables (set these in the Railway dashboard, never in chat or in git)

| Variable | Required | What it is |
|---|---|---|
| `BD_UI_PASSWORD` | yes | The sign-in password. 16+ characters or the app refuses to start. Changing it signs everyone out. |
| `ANTHROPIC_API_KEY` | for enrich/scan/brief | Your Anthropic API key. |
| `ANTHROPIC_WORKSPACE_ID` | only if the API says the key "is not scoped to a workspace" | The workspace id (from the Anthropic Console). The app sends it as the `anthropic-workspace-id` header. Not needed for a workspace-scoped key. |
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

1. Open the app and sign in. **Home** tells you the state and the one next click, and its **Setup** card lists anything missing in the Railway variables. Press **Test the connection** once: it tells you in plain words if the key, model or workspace needs fixing.
2. Press **Update my list** (tick the paid-calls box). One press researches the companies that need facts and applies the facts whose source checked out, searches new industry segments for Chicagoland companies, scans the ready companies (news, leadership, job boards, SEC filings, PE owners), and scores everything. It stops at the spending limit shown on the button.
3. Open **Opportunities** for the ranked list and the reasons behind each score. The numbers behind the button (companies to research, segments to search, companies to scan, spending limit) are on **Settings**.
4. Press the button again any time to refresh. Nothing runs by itself except the schedule below.

Every fact the update applies is on the **Change log** with its source link. **More → Advanced tools** still lets you run one step at a time.

The Monday 05:00 Chicago run (and the daily light refresh on other days) is separate and unchanged: it re-scans and re-scores, and posts to Teams only if `TEAMS_WEBHOOK_URL` is set.

## Operating notes

* Back up `/data/bd.sqlite` (the **Accounts** and **Exclusions** pages are the master copy; the CSVs in the image are only the first-boot seed).
* One job runs at a time; a job is stopped after 2 hours. If the service restarts mid-run the job shows as "interrupted" and the Monday catch-up (up to 12 hours late) starts it again if it never ran that week.
* A single replica only. Do not scale this service to more than one instance (SQLite, in-memory login throttle, scheduler).
* This is a public URL protected by one shared password. See SECURITY.md section 4b; ask IT about putting it behind SSO (Cloudflare Access, Tailscale, or SEI-managed hosting) before real use with confidential target lists.
