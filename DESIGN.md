# Notification client design interview

Status: Interview decisions settled; awaiting confirmation of the complete design before implementation.

## Settled requirements

- Standalone application at the project repository; fleet Compose deployment belongs in the dotfiles repository.
- Run through Docker Compose for one personal Pushover account.
- Receive notifications as a dedicated Open Client device, avoiding per-source configuration.
- Read/unread status is local to this application and separate from emergency acknowledgement.
- Preserve titles, bodies, timestamps, priorities, source applications and links. Image attachment preservation is excluded from the first version because the supported API does not expose image downloads. View images in the existing Pushover app.
- Allow local LAN access. Do not publish publicly or configure a tunnel, including Tailscale Serve/Funnel or Cloudflare Tunnel.
- Administrator login defaults to enabled, with an explicit Compose setting to disable it. Passwordless deployment grants history access and deletion capabilities to anyone able to reach the service.
- GUI: newest-first list, message detail, search, source and read-status filters, mark read/unread, selected deletion and clear history.
- Show receiver connection status and the last successful sync time.
- New notifications enter Inbox; Archive moves them to Archived; Restore returns them to Inbox. Delete permanently removes them from either view without a Trash view.
- Archive all moves Inbox notifications to Archived. Delete all history requires explicit confirmation, shows the affected count and permanently removes both Inbox and Archived history.
- Opening a notification marks it read. Archive and Restore preserve read status. There is no automatic expiry of stored history or attachments.
- First fleet deployment target: ThinkPad, alongside Healthchecks.
- Configure through the private GUI; discard the Pushover password after successful authentication and retain the receiving session credentials. Support Pushover 2FA.
- Retry collection automatically during outages, keeping saved history available. Save content before confirming receipt. Never automatically acknowledge emergency alerts.
- One application container with SQLite; keep persistent state in one mounted directory. No attachment download worker or attachment storage in the first version.
- Join ThinkPad's existing backup routine. Permanent deletion removes active history, not older backup copies; those expire under the backup policy.
- Emergency notifications use the ordinary list and detail views with a distinct indicator, such as an exclamation icon and light red background.
- No emergency acknowledgement control; use the existing Pushover app for acknowledgement.
- Reuse ThinkPad's existing private HTTPS routing and certificates for LAN and Tailscale access. Do not create a tunnel or public route.
- Responsive phone and desktop interface with automatic updates that preserve selected filters. Search across an explicit Inbox/Archived/All scope.
- Implementation stack selection is delegated: use a small maintained backend and server-rendered interface, without a separate frontend application.

## Reliability requirements

- Persist each received notification before confirming its queue delivery with Pushover. A failed database write must not confirm delivery.
- Deduplicate redelivery by Pushover's device message identifier. Repeated delivery must not undo local read status, archive state or permanent deletion.
- Keep minimal delivery bookkeeping separate from visible history so deleting history does not allow pending redelivery to restore deleted messages. Do not retain deleted notification bodies in that bookkeeping.
- Distinguish local read status, successful queue collection and emergency acknowledgement throughout the implementation.
- Show connection state and last successful sync without claiming an outage was fully recoverable after upstream retention expired.

## Confirmation gate

Confirm this document represents the complete intended first version before implementation. The interview's feature decisions do not authorize live fleet deployment; deployment follows the dotfiles repository's publish/apply workflow when requested.

## Verified upstream constraints

- The Open Client API does not document attachment retrieval. The official image-download feature request is marked Declined: [Open Client image request](https://support.pushover.net/i117-open-client-api-download-image-message). Do not promise attachment archiving through the supported API.
- Unconfirmed messages are retained upstream for at most 21 days; an extended collector outage can leave an unrecoverable gap. See [delivery policy](https://support.pushover.net/i39-notification-storage-and-delivery).
- Local read status and queue confirmation are separate from emergency acknowledgement.

## References

- [Domain vocabulary](GLOSSARY.md)
- [Receive notifications rather than intercept sends](dev-docs/adr/20261117-receive-notifications-for-history.md)
- [Pushover Open Client API](https://pushover.net/api/client)

The Open Client API requires a Desktop license after its trial. Receipt is limited to messages addressed to the client's device; old history on other devices cannot be imported through this API.
