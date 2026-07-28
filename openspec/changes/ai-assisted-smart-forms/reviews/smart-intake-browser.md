# Ego browser acceptance

Date: 2026-07-28
Task space: isolated `careerops smart intake` (Ego task space 32)
Frontend: Vite at `http://localhost:5173`
Backend: Compose API at `http://127.0.0.1:8000`
Data policy: no personal-data screenshots retained.

## Runtime boundary

The frontend and backend feature flags were left at their safe defaults:
`smart intake=false`, `MODEL_PROVIDER=disabled`, external writes=false,
auto-send=false, and Google OAuth=false. The browser acceptance therefore
targets the releasable manual-first states. Ready/invalid/stale provider-enabled
states are covered by fake-provider/unit/backend tests and are intentionally not
claimed as real-provider browser evidence.

## Assertions

At a desktop viewport of `1280x800`:

- `/profile` had `scrollWidth=1280`; the smart-intake text input and ordinary
  manual input were visible; the smart action was disabled; the manual save
  action was visible.
- `/ai-workbench` had `scrollWidth=1280`; human review messaging was visible;
  the interaction was manual-only; the separate Agent start action remained
  present; tabs were `职位匹配`, `简历审查`, `面试准备`, and `运行历史`.

At a narrow mobile viewport of `390x844`:

- `/profile` had `scrollWidth=390`; the smart action remained safely disabled
  and the manual path remained reachable.
- `/ai-workbench` had `scrollWidth=390`; human-review messaging remained
  visible without horizontal overflow.

The browser run also confirmed that the UI says `AI 结果必须人工确认`
instead of making an unsupported claim that a model is currently active or
closed. No browser action triggered a save, Agent start, crawl, send, OAuth, or
external write.
