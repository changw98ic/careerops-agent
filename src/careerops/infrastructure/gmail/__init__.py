"""Dedicated Gmail read-only runtime adapters."""

from careerops.infrastructure.gmail.client import (
    GMAIL_API_BASE_URL,
    GmailApiError,
    GmailHistoryCursorExpired,
    GmailHistoryPage,
    GmailListedMessage,
    GmailMessageListPage,
    GmailMetadataMessage,
    GmailProfile,
    GmailReadOnlyHttpClient,
)
from careerops.infrastructure.gmail.credentials import (
    GMAIL_READONLY_SCOPE,
    GmailAccessToken,
    GmailBrokerCredentialEnvelope,
    GmailCredentialBrokerError,
    GmailCredentialHandle,
    UnixGmailCredentialBroker,
)

__all__ = [
    "GMAIL_API_BASE_URL",
    "GMAIL_READONLY_SCOPE",
    "GmailAccessToken",
    "GmailApiError",
    "GmailBrokerCredentialEnvelope",
    "GmailCredentialBrokerError",
    "GmailCredentialHandle",
    "GmailHistoryCursorExpired",
    "GmailHistoryPage",
    "GmailListedMessage",
    "GmailMessageListPage",
    "GmailMetadataMessage",
    "GmailProfile",
    "GmailReadOnlyHttpClient",
    "UnixGmailCredentialBroker",
]
