# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

---

## Trading Journal (`artifacts/trading-journal`)

React + Vite SPA with dark navy/teal theme. Auth via Clerk (live key — works only after publishing; dev shows 5s spinner then LandingPage).

### Features implemented
- **Guest mode** — localStorage store (`tj_guest_*` keys), amber banner, "Continua come Ospite" button on LandingPage
- **Guest → Account migration** — `GuestMigrationDialog` in `App.tsx`: after Clerk login, if guest trades exist in localStorage, prompts user to import or discard
- **Clerk Google auth** — sign-in/sign-up pages, `ProtectedRoute`, `HomeRedirect`
- **Trades CRUD** — via API server (`/api/trades`); guest mode uses localStorage via `@/lib/store`
- **Journal page** — list with filters: symbol, outcome, folder, direction, session, date range (from/to). CSV export + CSV import (client-side parse)
- **Screenshot URL** — optional image URL field in add-trade/edit-trade forms with live preview; displayed in trade-detail
- **Dashboard** — equity curve (AreaChart), win rate pie chart, P&L calendar heatmap (GitHub-style, last 26 weeks), recent trades list
- **Stats page** — equity curve with drawdown overlay, P&L by symbol, P&L by day-of-week, rolling win rate (10-trade window), P&L by session, best/worst trade, streaks, profit factor, max drawdown
- **Position Size Calculator** — `/calculator` page: account size, risk %, entry, SL, TP, pip value → lot size, risk amount, potential profit, R:R ratio
- **Global search** — CMD+K modal in layout, searches by symbol/notes/tags, navigates to trade detail
- **Community similarity** — `communitySignatures` DB table, anonymous user hash, `POST /api/community/sync` on trade save
- **MT5/MetaAPI auto-sync** — `/mt5` page, daily scheduler
- **Telegram bot** — notify on trade save/edit, `/stats` command, cache push
- **Folders** — organize trades by folder (created in Settings)
- **i18n** — IT/EN toggle in sidebar
- **Wyckoff phases + Market phases** — dropdowns in add/edit trade forms

### Key files
- `src/App.tsx` — routing, Clerk provider, `GuestMigrationDialog`
- `src/components/layout.tsx` — sidebar nav (incl. Calculator), global search modal, guest banner
- `src/pages/dashboard.tsx` — KPI cards, equity curve, P&L calendar heatmap
- `src/pages/stats.tsx` — detailed stats with drawdown chart
- `src/pages/journal.tsx` — trade list, all filters, CSV import/export
- `src/pages/calculator.tsx` — position size calculator
- `src/pages/add-trade.tsx` / `edit-trade.tsx` — trade forms with screenshot URL
- `src/pages/trade-detail.tsx` — trade detail with screenshot display
- `src/lib/store.ts` — Zustand store (API for auth users, localStorage for guests)
- `src/lib/guest.ts` — guest mode helpers

### Auth note
Clerk live publishable key is set. Auth (sign-in/sign-up with Google) only works in the published (deployed) environment. In dev, Clerk times out after 5s and shows the LandingPage.

---

## FinCoach (`artifacts/fincoach`)

React + Vite SPA. Standalone personal finance tracker + AI coaching app. Emerald-green theme. Auth via Clerk (`VITE_CLERK_PUBLISHABLE_KEY`). Preview path: `/fincoach/`.

### Features implemented
- **Expense/income tracking** — add, edit, delete transactions by category, type, date; month filter
- **Financial goals** — create goals with target/current amount, deadline, category; contribution flow; progress bars
- **Dashboard** — monthly income/expense/balance summary, budget usage bar, daily trend chart (Recharts AreaChart), category breakdown pie chart, active goals overview
- **AI Finance Coach** — chat interface powered by OpenAI GPT-4o-mini; uses user's real spending data as context; free users limited to 5 messages/month, premium = unlimited
- **Premium system** — €2.99/month via PayPal + Telegram bot @Forexrama_bot `/premium` command; admin endpoint `POST /api/fincoach/admin/grant-premium` (x-admin-secret header)
- **AdSense ads** — shown on dashboard, expenses, goals for non-premium users (ca-pub-8048780500626071)
- **Settings** — currency selector, monthly budget; AI message usage counter

### DB tables (Drizzle, PostgreSQL)
- `fc_profile` — userId, currency, monthlyBudget, isPremium, premiumSince, aiMessageCount
- `fc_expenses` — id, userId, amount, type, category, description, date
- `fc_goals` — id, userId, name, targetAmount, currentAmount, deadline, category, status
- `fc_ai_messages` — id, userId, role, content

### Key API routes (all under `/api/fincoach/`)
- `GET/PUT /profile` — user profile
- `GET/POST /expenses`, `PUT/DELETE /expenses/:id`
- `GET/POST /goals`, `PUT/DELETE /goals/:id`, `POST /goals/:id/contribute`
- `GET /dashboard?month=YYYY-MM` — summary stats
- `POST /ai/chat`, `GET /ai/history` — AI coaching
- `POST /admin/grant-premium` — admin premium grant

### Key files
- `artifacts/fincoach/src/App.tsx` — ClerkProvider, QueryClient, Wouter router
- `artifacts/fincoach/src/components/layout.tsx` — sidebar nav, mobile header
- `artifacts/fincoach/src/pages/` — landing, dashboard, expenses, goals, coach, settings, premium
- `artifacts/api-server/src/routes/fincoach.ts` — all FinCoach API routes
