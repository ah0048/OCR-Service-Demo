# EGX ID OCR: processing monitor (Angular)

Live dashboard for the OCR demo pipeline in `../backend`. Everything updates in realtime (server-sent events, no refresh).

## Run
```powershell
# backend first (see ../backend/README.md): docker compose, API, workers
cd frontend
npm install
npm start          # http://localhost:4200
```
The API is expected at `http://localhost:5125` (change `API_URL` in `src/app/api.ts`).

## Pages
- **Overview**: KPIs, animated pipeline (API → ocr-jobs → OCR workers → ocr-results → business workers → PostgreSQL), throughput and backlog charts, batch progress, outcomes, latest jobs.
- **Results**: a live feed of finished jobs, one row each: image placeholder, job id / image reference, outcome, confidence, and the OCR output as JSON. New rows are pushed in as each job completes (Pause freezes the list; filter by outcome). The placeholder is `components/image-placeholder`; pass it a `src` when real images exist.
- **Queues & workers**: every RabbitMQ queue (ready / unacked / rates / dead letters) and every worker (online, busy, processed, share).
- **Jobs**: the `jobs` table with search, state/outcome filters, paging. Click a row for the job drawer (stepper, progress, timing breakdown, reason codes, mock extracted fields).
- **Batches**: segmented progress bars with speed and ETA.
- **Submit jobs** (top bar): one job, or a batch of up to 10,000.
- The bell shows alerts: broker unreachable, no workers online while work waits, dead-lettered messages.

Build: `npm run build` (output in `dist/`).

## Structure
```
src/app/
  core/         api.ts (HTTP client), dashboard-store.ts (SSE stream + histories), models.ts, fmt.ts
  components/   reusable pieces, one folder each: navbar, batch-card, jobs-table, job-drawer,
                submit-dialog, state-chip, seg-bar, donut, line-chart
  pages/        routed pages, one folder each: overview, pipeline, jobs, batches
  app.*         root component, routes, config
```
Each component folder holds `name.ts`, `name.html` and `name.scss`. Global design tokens and shared classes (cards, buttons, chips, tables, bars) live in `src/styles.scss`.

## Large, clear display
Built for people who need big text and for wall-sized or full-HD screens:
- The page uses the **full screen width** (no maximum width).
- Everything is sized in `rem`, and the root font size follows the screen width (about 23px on a 1920px display, 17px on a laptop). One rule in `src/styles.scss` controls it.
- **A- / A+ in the header** lets each person pick their own size (90% to 170%); the choice is remembered in the browser (`core/ui-settings.ts`).
- Layout breakpoints are **container queries in rem** (not screen pixels), so a larger text size automatically gets fewer columns instead of overflowing; the live pipeline diagram turns vertical when it runs out of room.
- Greys were darkened for contrast and keyboard focus is clearly outlined.
Text inside SVG drawings (the donut) is sized in SVG units on purpose.
