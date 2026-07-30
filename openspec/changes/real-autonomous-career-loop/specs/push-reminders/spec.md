## ADDED Requirements

### Requirement: Reminders are pushed to the user through a notification outlet
The system SHALL provide a notification outlet (in-app and server-sent events) that delivers reminders to the user in real time. The system SHALL NOT rely solely on the user manually refreshing a page to discover reminders.

#### Scenario: Follow-up reminder is pushed
- **WHEN** an application passes its follow-up due date with no response
- **THEN** the system pushes a follow-up reminder through the notification outlet

#### Scenario: User has the page open
- **WHEN** a reminder fires while the user's page is open
- **THEN** the reminder appears without a manual refresh via the server-sent events channel

### Requirement: Reminder rules fire on real state
The system SHALL evaluate reminder rules (follow-up due dates, upcoming interviews, stale applications) against current application and mail state on the proactive-trigger-loop cadence, and SHALL fire a reminder only when the rule's condition is met.

#### Scenario: Interview is upcoming
- **WHEN** an interview is scheduled within the reminder lead window
- **THEN** the system pushes an interview reminder carrying the interview details

#### Scenario: Stale data is not presented as current
- **WHEN** mail sync has not run within the staleness window
- **THEN** the reminder surface indicates the data may be stale rather than presenting silence as "nothing due"
