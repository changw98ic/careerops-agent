# Spec Review 1 — Product Flow and User Journey

## Review method

This pass reviewed the seven capability specs from the user's point of view. It traced one application from profile setup through crawl, discovery, preparation, delivery, inbound mail, reply, and follow-up. It did not rely on the implementation design or task plan.

## Findings

### 1. Primary journey is covered

The specs contain a complete chain:

`career profile → crawl plan → job inbox → application → package → channel → delivery → recruiting mail → proposed event → reply/follow-up`.

The user-visible control points are present at preference activation, job selection, package approval, email send confirmation, mail-event review, and reply approval.

### 2. The application is correctly the central object

`application-workspace` links candidate, canonical job, source posting, package, channel, provider identifiers, timeline, and reminders. This prevents the product from becoming a collection of disconnected Agent outputs.

### 3. Email is correctly modeled as a delivery channel and a follow-up thread

`email-application-delivery` covers initial applications and system-managed replies. `recruiting-email-intelligence` links inbound threads back to the application. The user clarification is represented explicitly: approval happens inside CareerOps and the backend performs the send; the user is not sent to Gmail to finish the action.

### 4. One state ambiguity was found and corrected

The first draft made a favorite-created application enter `PREPARING` in one scenario while the discovery spec used `FAVORITED`. The application spec now keeps creation in `FAVORITED` and treats entering the preparation workspace as a separate legal transition.

### 5. Crawl management is a product capability, not an implementation detail

`crawl-plan-management` gives the user ownership of sources, themes, filters, frequency, time windows, limits, pause/resume, run history, and safe failures. This is sufficient to represent the requested “主题、间隔、范围” controls.

### 6. Resume tailoring is bounded enough for a first product contract

The specs require immutable resume versions, evidence-bound claims, diffs, and explicit package approval. They do not allow silent rewriting or invented experience.

## Review decision

PASS, with the state correction applied. The product-flow spec is coherent enough to proceed to an architecture design. The design must resolve the migration from the current read-heavy MVP scope and must make the UI review queue a first-class surface.
