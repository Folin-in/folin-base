# Analysis-complete notifications

Successful queued uploads insert a privacy-safe outbox entry in the same transaction
as the report and job success. Rejections, timeouts and rollbacks create no event.
The initial report is ready before optional background verification finishes; the
notification does not claim it has been verified.

The completion worker scans every five seconds while the backend is running.
Android sends directly through Firebase Cloud Messaging; legacy Expo registrations
retain their existing transport and receipt handling. Each registered
device gets a database delivery claim keyed by report ID, preventing ordinary
duplicate sends across scans/processes. As with other push systems, a crash after
provider acceptance but before saving the ticket can still cause a duplicate retry.
Transient failures back off five minutes; events expire after 24 hours. Delivery is
not guaranteed or instantaneous, and an Expo receipt is not proof the user saw it.

Completion alerts apply to Free and Pro accounts when notifications are daily or
weekly; that cadence only applies to retest reminders. Off suppresses both types.
Devices registered after completion do not receive old completion alerts. Deleted
reports are excluded. No values, names, lab names or filenames appear on the lock
screen. Tapping an alert opens only a validated internal report route; the API still
enforces ownership.

Android uses the `analysis-results` channel. Firebase client configuration, server
push credentials, notification permission and a registered device are still required.
The Android emulator passed a real FCM push/display/tap-to-report test on 2026-09-11;
see `firebase-push-setup.md` for scope. Physical-device/release testing remains.
