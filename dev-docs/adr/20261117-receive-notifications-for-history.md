# Receive notifications rather than intercept sends

Date: 2026-11-17
Status: Accepted

## Context

The user wants a personal history covering notifications from existing and future Pushover source applications without adding each source to a separate logging system. Logging at each sender would require integration changes and would record attempts rather than receipt by a dedicated client.

## Decision

Register a dedicated device through Pushover's Open Client API and build local history from notifications delivered to that device. Keep read status, Inbox/Archived membership and deletion local to this client. Queue confirmation is separate from emergency acknowledgement, which remains in the existing Pushover app.

## Rationale

Receiving provides a common collection point across source applications without changing their sending integrations. Local history operations do not change the user's phone history or silence emergency alerts.

## Consequences

- Notifications addressed exclusively to other devices are outside the history.
- The client cannot import historical messages already delivered to other devices.
- Outages longer than Pushover's pending-message retention can cause permanent gaps.
- Image archiving is excluded from the first version because the supported Open Client API does not expose image downloads.
- A Pushover Desktop license is required after the Open Client trial.
- The archive records receipt by its own device, not failed sending attempts or proof that a person read a message.

## References

- [Open Client API](https://pushover.net/api/client)
- [Notification storage and delivery](https://support.pushover.net/i39-notification-storage-and-delivery)
- [Image download feature request](https://support.pushover.net/i117-open-client-api-download-image-message)
