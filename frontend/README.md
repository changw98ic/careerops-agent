# CareerOps Frontend

Vue 3 SPA for the CareerOps job-search console. Uses Ant Design Vue 4 for UI components and AntV G2 for the dashboard chart. The UI is review-first -- it can inspect jobs and application state, but external writes are controlled by the backend policy kernel.

## Tech Stack

- **Vue 3** with `<script setup>` composition API
- **Vue Router 4** with history mode and lazy-loaded routes
- **Ant Design Vue 4** (tree-shaken, individual component imports)
- **AntV G2** for data-driven charts
- **Vite 8** for dev server and production build
- **Vitest** + `@vue/test-utils` for unit tests

## Development Setup

### Prerequisites

- Node.js >= 18
- The CareerOps backend running at `http://127.0.0.1:8000` (see `../backend/`)

### Install and Run

```bash
npm install
npm run dev
```

The Vite dev server starts at `http://127.0.0.1:5173`. It proxies only API requests to the
backend; browser pages stay in the Vue application:

| Path | Target |
|------|--------|
| `/api/*` | `http://127.0.0.1:8000` |

The smart form entry point is default-deny. Set `VITE_SMART_INTAKE_ENABLED=true` only in a
frontend build paired with backend `CAREEROPS_SMART_INTAKE_ENABLED=true`; otherwise Profile and
AgentWorkbench show the manual fallback and do not present an interactive AI CTA.

## Bootstrap and Session Flow

### First-Time Bootstrap

1. The backend generates a one-time `bootstrap_token` on first startup (printed to stdout).
2. Navigate to `http://127.0.0.1:5173/bootstrap` in both development and Compose.
3. Enter the token, choose a username and password.
4. The frontend calls `POST /api/v1/auth/bootstrap` which creates the initial admin user.
5. On success the session is established and the user is redirected to the dashboard.

### Session Lifecycle

1. On app load, `App.vue` calls `checkSession()` which hits `GET /api/v1/auth/session`.
2. The backend reads the HttpOnly session cookie and returns the current user state.
3. The router guard (`router.beforeEach`) blocks rendering until the session check completes.
4. Session status is one of: `unknown` -> `checking` -> `authenticated` | `anonymous`.
5. Protected routes (`meta.auth = true`) redirect to `/login` when the session is anonymous.
6. Authenticated users visiting `/login` are redirected to the dashboard.

### Login Flow

1. `POST /api/v1/auth/preauth` obtains a short-lived CSRF token.
2. `POST /api/v1/auth/login` sends credentials and the CSRF token.
3. On success the session cookie is set and session state updates to `authenticated`.

### Logout

`POST /api/v1/auth/logout` invalidates the server session. Local state is cleared and the browser redirects to `/login`.

## API Integration

All API calls go through `/api/v1/...` which Vite proxies to the backend. The key endpoints consumed by the frontend are:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/auth/session` | GET | Check current session |
| `/api/v1/auth/preauth` | POST | Obtain CSRF token |
| `/api/v1/auth/login` | POST | Authenticate |
| `/api/v1/auth/logout` | POST | End session |
| `/api/v1/auth/bootstrap` | POST | First-time admin setup |
| `/api/v1/jobs` | GET | List jobs |
| `/api/v1/jobs/:id` | GET | Job detail |
| `/api/v1/companies` | GET | List companies |
| `/api/v1/applications` | GET | List applications |
| `/api/v1/dashboard/stats` | GET | Dashboard aggregates |
| `/api/v1/smart-intake/previews` | POST | Create/reuse a review-only smart form preview |
| `/api/v1/smart-intake/previews/:id` | GET | Read a candidate-owned preview |
| `/api/v1/smart-intake/previews/:id/apply` | POST | Record decisions and return a local draft patch |

The API client (`src/api/client.js`) automatically attaches the CSRF token to mutating requests via the `X-CSRF-Token` header.

## Project Structure

```
frontend/
  src/
    api/            # API client and endpoint functions
    stores/         # Reactive state (session.js)
    views/          # Route-level components
      Dashboard.vue
      Jobs.vue
      JobDetail.vue
      Companies.vue
      Applications.vue
      Login.vue
      Bootstrap.vue
      NotFound.vue
    App.vue         # Root layout with sidebar and header
    main.js         # Component registration and app mount
    router.js       # Route definitions and auth guards
    styles.css      # Global styles
  vite.config.js    # Dev proxy and build config
  index.html        # SPA entry point
  package.json
```

## Routes

| Path | Component | Auth | Description |
|------|-----------|------|-------------|
| `/` | redirect | -- | Redirects to `/dashboard` |
| `/dashboard` | Dashboard | yes | Overview chart and stats |
| `/jobs` | Jobs | yes | Job inbox listing |
| `/jobs/:id` | JobDetail | yes | Single job view |
| `/companies` | Companies | yes | Company listing |
| `/applications` | Applications | yes | Application tracking |
| `/login` | Login | no | Authentication form |
| `/bootstrap` | Bootstrap | no | First-time admin setup |
| `/404` | NotFound | no | Not found page |

All routes use lazy loading (`() => import(...)`) except the redirect.

## Testing

```bash
# Run all tests once
npm test

# Run in watch mode
npm run test:watch
```

Tests use Vitest with `happy-dom` as the DOM environment. Test files live alongside the code they cover or in a `__tests__/` directory.

## Build and Deployment

### Production Build

```bash
npm run build
```

Output goes to `dist/`. The frontend service serves this static SPA with history-mode fallback
and forwards `/api/*` to the backend. The browser therefore uses one origin for pages and API
calls even though the processes are separate.

### Preview Locally

```bash
npm run preview
```

Serves the production build at `http://localhost:4173`.

### Deployment Notes

- Use `npm ci` in CI/CD environments for reproducible installs.
- The frontend proxy must target the backend through `VITE_API_PROXY_TARGET`; browser code must
  continue using relative `/api/...` paths.
- History-mode routing requires the web server to rewrite all non-asset paths to `index.html`.

## UI Boundaries

- `ant-design-vue` provides layout, forms, tables, feedback, and navigation.
- `@antv/g2` provides the data-driven chart on the dashboard.
- API contracts are defined by the FastAPI backend; the frontend does not enforce authorization or side-effect policy.
