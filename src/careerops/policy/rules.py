RULE_VERSION = "m0.default-deny.v1"

READ_ACTIONS = frozenset({"read_resource", "list_resource"})
INTERNAL_ACTIONS = frozenset({"create_internal_draft"})
EXTERNAL_WRITE_ACTIONS = frozenset({"send_email", "create_calendar_event"})
