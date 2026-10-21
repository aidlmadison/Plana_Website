# Canvas → AI evaluation → Plana

## Each batch

1. Download the batch submission ZIP from Canvas and extract the student PDFs into `C:\Qi Research\Plana_DrawingEvaluation\data\pdfs`. Keep original filenames. PDFs must be directly in this folder. Keep only this batch's chosen submission per student; the filename prefix before `_` is the login account.
2. Open the evaluation run panel, click **Refresh files**, select the PDFs, and click **Start Detection**. The panel saves `outputs/web_runs/evaluation_<date_time>_<suffix>/`. The CLI and `run.py` save the same layout under `outputs/`. Inspect warnings and failed/partial pages before releasing feedback.
3. The complete run is copied unchanged to the sibling website's `imports/<same folder name>/` automatically. In the local instructor portal, click **Check import folders**. You can also copy/paste the whole run folder there yourself, without opening or editing anything inside it.
4. For the hosted website, expand **Website handoff — complete evaluation folder** in the run panel, click **Prepare evaluation folder ZIP**, then **Download evaluation folder ZIP**. Open the hosted instructor portal and use **Import evaluation ZIP**. The same unchanged folder is inside the ZIP.
5. Review the students' imported submissions. Open **Student credentials**, upload or edit the roster (`identifier,name,email`), save it, and download the credentials CSV from that hosted website for your manual email agent. Roster name is the student's full/display name. Confirm names/emails against the class roster. Do not distribute the full credentials CSV to students.

## One portable run format

```
evaluation_2026_10_21_14_30_00_a1b2c3/
  input/                         original student PDFs, unchanged filenames
  reference/mistakes_catalog.csv catalog snapshot used for this run
  prompts/                       evaluation prompt snapshots
  results.json                   complete batch AI results
  results.csv                    tabular results
  results_run.json               model/settings and page outcomes
  pages/                         annotated page PDFs when enabled
  run_manifest.json              completion fingerprint
```

Copy the outer folder, including all its contents. The website uses original PDFs and the AI bounding boxes, not the pre-annotated PDF as the drawing source. The catalog shown to students is the run's own snapshot, even if the evaluation repository catalog changes later. Extra comparison artifacts can stay inside the run.

Repeated imports of the same PDF/results/catalog content preserve submission identities and reviews. A new evaluated result is a new assignment; existing students keep their accounts and keys. Keep a completed run unchanged. Start a new evaluation to change AI results. Earlier per-student `data/imports` bundles are compatible; `latest.json` is no longer required.

## GitHub and hosting

Push the website application code to its GitHub repository, then connect that repository to a Python/container host with HTTPS and persistent storage. GitHub Pages supports static files and cannot run this Python server, authentication, or SQLite database.

Deploying code and updating student evaluations are separate actions. Redeploy code when application files change; upload a new evaluation ZIP when a batch is ready. Do not commit `imports/`, `data/`, PDFs, login keys, `.env`, or the SQLite database. They are ignored by Git.

`PLANA_DATA_DIR` selects private persistent storage (default `data/`). `PLANA_IMPORTS_DIR` selects the import inbox (default `imports/`). The app also scans older `PLANA_DATA_DIR/imports` bundles. An uploaded ZIP goes into the inbox and the website keeps an owned private copy in `PLANA_DATA_DIR/evaluations/`. Removing the inbox copy does not remove a registered submission.

The included Dockerfile runs the app on port 8000. `compose.yaml` maps it only to local port 8765 and stores runtime data in a named volume. Put a managed HTTPS proxy in front, preserve Host, configure `PLANA_HTTPS=1` for HTTPS cookies, and allow uploads up to 250 MB with a suitable proxy timeout. The host/provider setup has not been deployed or verified here. Use one application instance for this SQLite setup.

Persist the same data volume across every code deployment. Back up the database, private credential files, and cached evaluations together, with the app stopped or through a consistent database backup. Do not replace host data with the local checkout on deployment. The hosted app is authoritative for reviews and credentials: new independent host storage generates new keys unless you explicitly migrate the existing private data during initial setup. Send credentials downloaded from the live host.

## Troubleshooting

- No new submissions: use **Check import folders** after copying, or upload the ZIP on the actual hosted website. Local imports do not synchronize to a remote server through Git.
- Incomplete run: verify original PDFs are in `input/`, each has a matching results entry, and the catalog is in `reference/`. Older runs may need a one-time backfill using their original PDFs and catalog; do not substitute a changed catalog.
- A run changed after finalization: keep the completed run intact and create a new run. Never manually edit an imported run to change a student decision; use the instructor portal.
- Missing names/emails: import the roster. A PDF filename supplies an account identifier, not a verified name/email.
