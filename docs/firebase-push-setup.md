# Android push: direct Firebase Cloud Messaging

Android now registers native FCM tokens, not Expo push tokens. The backend sends
FCM HTTP v1 requests directly. Supabase remains the database. No Expo account or
Expo project ID is needed for this Android delivery path. Legacy Expo registrations
are provider-tagged so they cannot accidentally be sent to Firebase.

## Account setup

1. Sign in to Firebase Console, select the intended Folin project and register an
   Android app with package `com.folin.app`.
2. Download its Android **google-services.json** into `folin-handy/google-services.json`.
   This is client configuration, NOT the private service-account key. Dynamic Expo
   config and the existing Gradle project recognize this file.
3. Enable the Firebase Cloud Messaging API (v1). Supply a service account authorized
   for Firebase Cloud Messaging (prefer the messaging-specific role, not Owner).
   Keep its private JSON outside the mobile app and source control. Configure the
   backend's `FIREBASE_SERVICE_ACCOUNT_FILE` with its absolute path, then restart.
   No Firebase Auth/Firestore/Storage or paid Cloud Functions are needed here.
4. Rebuild and install Android, sign in, allow notifications and leave the preference
   enabled. The app must successfully register an active `provider=fcm` device token.
5. Upload a synthetic report, background the app, observe the system notification,
   and tap it. Confirm it opens the correct report. Also test foreground receipt.

An FCM `accepted` record means Google accepted the send, not proof that a device
displayed it. Real delivery must be checked on the emulator/device as well.
Unregistered devices are deactivated; transient failures keep the existing retry
behavior. No medical content appears in notification text.

## Local verification — 2026-09-11

Configured folin-prod Android client and server credentials. Android debug build
installed successfully and registered an active native FCM token. Sent a clearly
labelled test through the real backend FCM transport while the app was backgrounded:
the notification appeared in the Android system shade and tapping it opened the
target saved report. This tested real transport/display/routing, not a fresh AI
upload or the daily/weekly reminder schedule. Those triggers have automated checks.
Foreground delivery and production-build delivery still require separate validation.
Server credential location is configured only in the ignored local environment;
never commit or embed that private JSON in the app.

References: [FCM HTTP v1](https://firebase.google.com/docs/cloud-messaging/send/v1-api),
[native tokens with expo-notifications](https://docs.expo.dev/push-notifications/sending-notifications-custom/).
