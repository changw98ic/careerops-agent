## ADDED Requirements

### Requirement: A dual-model OA approval loop replaces per-message human review
The system SHALL replace the per-message human-approval gate (the `review_gate` interrupt and the mandatory kernel approval stage) with an autonomous generate-review-revise loop. Drafter model A composes the message on top of a template skeleton grounded in trusted business state (job / resume / contact); reviewer model B reviews A's draft along an orthogonal axis (wording risk, recipient correctness, over-promise). Both roles SHALL use the same MiMo model via two independent calls with distinct prompts and NO shared context on the first review pass.

#### Scenario: Approved draft is sent autonomously
- **WHEN** reviewer B approves A's draft
- **THEN** the system sends the message through the side-effect chain without a human approval click and records it on the timeline as agent-initiated

#### Scenario: Rejected draft is revised and resubmitted
- **WHEN** reviewer B rejects A's draft with feedback
- **THEN** B's feedback is passed back to A, A revises the draft, and the revised draft is resubmitted to B for re-review

#### Scenario: Reviews are independent on the first pass
- **WHEN** B reviews A's draft
- **THEN** B sees only the draft itself, never A's self-assessment, so the two judgements are not anchored to each other

### Requirement: The approval loop terminates and escalates rather than looping forever
The generate-review-revise loop SHALL run at most 5 rounds. If reviewer B has not approved after 5 rounds, the system SHALL escalate the draft to human review instead of auto-sending or confidence-degrading the send. Human review is a fallback for the rare non-converging case, not a per-message step.

#### Scenario: Five rounds fail to converge
- **WHEN** A and B have not converged after 5 revision rounds
- **THEN** the draft is escalated to human review and is NOT sent autonomously

#### Scenario: Convergence within budget
- **WHEN** B approves within 5 rounds
- **THEN** the message is sent without escalation

### Requirement: Same-model dual-role is accepted for low-stakes templated sends
Because outbound messages are template-grounded and the worst-case outcome is recipient blacklisting, the system SHALL accept the same MiMo model in both roles rather than requiring a second vendor model. The trade-off is explicit: this guards against single-call error and angle-blindness, not against a model-level systematic blind spot.

#### Scenario: A single vendor backs both roles
- **WHEN** only the MiMo provider is configured
- **THEN** the loop still runs with A and B as two independent calls of that model under distinct prompts
