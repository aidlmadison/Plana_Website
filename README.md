# Plana drawing review

A local web app for students and instructors to review evaluated PDF drawings together. The server stores accounts, review requests, instructor decisions, and an audit trail in SQLite. All PDF and evaluation routes require authentication; students can access only their own submissions. Two instructor accounts share all submissions.

## Start

From this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py serve
```

Open http://127.0.0.1:8765. Or double-click **Start Plana.cmd** after setup. `python app.py import` imports data without starting the server. Change the initial instructor count with `--instructors 2`; this creates missing accounts without resetting existing ones.

Private initial keys are generated at first import in `data/private/login_keys.csv`. Open that file locally for instructor credentials and initial student access. The private database stores salted password hashes and access keys for the instructor credentials panel. Both must remain private. Neither keys nor student data are tracked in Git. Do not distribute the credential file to students.

## Evaluation handoff

Use the [complete workflow guide](docs/WORKFLOW.md). New evaluation runs are self-contained `evaluation_<date_time>_<suffix>` folders. Copy the whole folder unchanged into `imports/`, then click **Check import folders** or restart the website. No `latest.json`, per-student split, import-file edits, or AI rerun is needed.

For a hosted website, use the run panel's **Prepare evaluation folder ZIP** and **Download evaluation folder ZIP**, then sign in as an instructor and click **Import evaluation ZIP**. Uploads are limited to 250 MB compressed / 1 GB extracted. The folder is validated before registration. Invalid or incomplete runs show an error. Earlier `data/imports` bundles remain supported.

Identifiers are the lowercased PDF filename prefix before the first underscore. `piermaeryn_618132_...pdf` becomes account `piermaeryn`. Each student must have one PDF per run. Names and emails come from the roster, never from filename guesses.

The website keeps imported PDFs and catalog snapshots in private persistent storage under `data/evaluations/`. Content-based identities prevent duplicate submissions when a folder is copied, renamed, or uploaded again. Existing accounts, keys, rebuttals, self-reports, instructor decisions, and audit records are preserved. Changed evaluations create separate submissions.

## Review workflow

In the student portal, **Instructor reviewed** shows only requests with a completed instructor decision (agreed or disagreed), across all pages. Open, pending, and canceled findings are excluded from that filter. Instructors retain **Awaiting review** for pending requests.

The **Mistake catalog** button beside **Grading rules** opens a floating reference window for students and instructors. It shows every catalog entry as a card with its ID, full description, theme, and example. Search by ID or text; close with the × button or Escape.

- Students expand an AI finding to see its explanation, catalog definition, confidence, and highlighted regions. At the bottom they can open a rebuttal entry and submit it. A pending rebuttal is orange; clicking its cancel button returns it to the original AI state.
- **Report unidentified mistake** asks for one drawn box, then a description and an optional catalog ID. A pending self-report is orange and can be canceled or have its single box replaced.
- Instructors choose a submission, expand a pending request, write feedback, and agree or disagree **with the student**. Agreeing accepts the rebuttal or validates the self-report. Decisions and feedback are visible to the student and both instructors. A stale simultaneous decision is rejected and requires a refresh.
- An accepted rebuttal keeps a red finding with struck-through title. An accepted self-report is green. Rejected requests are red with `!`. Instructors can use **Edit decision** on a reviewed request to update their feedback and save it as agreed or disagreed. Status, feedback, and scores update for students and both instructors. Prior decisions remain in the audit trail; stale simultaneous edits are rejected. Only pending requests can be canceled by students.
- Feedback refreshes every 15 seconds when there is no active draft, or immediately with **Refresh feedback**. Bounding boxes use full-page `[ymin, xmin, ymax, xmax]` coordinates normalized to 0–1000; zooming does not change them.

## Grading

Edit `grading.json` to change grading without changing code. The current rules use a maximum of 60, base deduction of 1, and repeat multiplier of 0.5. Occurrences sharing a catalog mistake ID deduct 1, 0.5, 0.25… across the entire submission. One AI finding counts once regardless of the number of boxes. The original score includes all AI findings; the adjusted score excludes accepted rebuttals and recalculates the decay.

Accepted self-reports provisionally add 1, 0.5, 0.25… per catalog ID, controlled by `self_report_credit`. Unclassified reports share one repeat group. Bonus credit is capped at the maximum score. Pending and rejected requests do not change the score. The total is 60 points. Self-report credit remains configurable through `self_report_credit`.

## Student credentials and manual email delivery

Instructors open **Student credentials** beside the account badge. The simple reference lists each student's PDF identifier, roster name, email, and login key. Use **Show login keys** to reveal credentials, or **Download credentials CSV** to prepare emails using another tool. Automatic account creation during PDF import is unchanged.

Existing roster information and login keys are preserved. Names and emails can be edited and saved here, or imported from a roster CSV with `identifier,name,email` headers. Identifiers must match imported PDFs; names and valid `wisc.edu` email addresses are required for edited/imported rows. No email address is inferred for missing roster entries.

A private local copy is kept at `data/private/student_credentials.csv`, with columns `identifier,name,email,key`. It refreshes at startup, after roster updates and PDF imports, and when downloading. It includes all imported student accounts, even when an email is still missing. This file is ignored by Git and is suitable as input to your manual email drafting workflow.

The previous Student access invitation panel, recipient confirmation checkboxes, mail configuration notices, email previews, and send controls have been removed. The web email-send endpoint is disabled. No mail configuration or public URL is needed to view or download credentials. The roster CLI remains available with `python app.py roster --file roster.csv`.

## Hosting

The default server listens only on this computer. It is a local application, not an Internet deployment. Before students connect remotely, host it behind a managed HTTPS reverse proxy with a persistent private data volume, set `PLANA_HTTPS=1` for secure cookies, and restrict filesystem access to `data/` to the server operator. Keep the proxy's Host and Origin consistent. Bind to a private service interface using `--host` as needed. Do not expose the standard-library development server directly to the Internet. A provider-neutral Dockerfile and compose.yaml are included. Configure the chosen host, HTTPS reverse proxy, and persistent storage before inviting students. GitHub stores/deploys application code; private student data is uploaded to the running application, not committed to Git. GitHub Pages cannot run this Python application.

Sessions expire after 12 hours. Cookies are HttpOnly and SameSite=Strict, writes require session CSRF tokens, login attempts are rate limited, and student/instructor permissions are checked on every private route. Back up the SQLite database and imported PDFs together. Key rotation/recovery and institutional SSO are not implemented in this initial version.

## Verify

```powershell
python -m unittest discover -s tests -v
node --check static/app.js
```

Tests use temporary data and synthetic PDFs; they do not call Gemini or send email.
