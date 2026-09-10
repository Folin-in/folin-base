# Google Play billing launch checklist

Folin's Android app uses Google Play Billing for Pro. The mobile app never grants
Pro itself: it sends the Play purchase token to the API, which verifies and
acknowledges it with Google before updating the user's entitlement.

## Play Console

1. Create the subscription under **Monetize → Products → Subscriptions**. Product
   IDs cannot be reused or changed later, so decide the ID before creating it.
2. Create and activate the base plan, price it for India, and add any other
   countries you intend to support. The app displays Google's localized price.
3. Give the backend service account access to the app and permission to manage
   orders/subscriptions, then enable the Google Play Android Developer API.
4. Upload a signed AAB to an internal-testing track. Billing product queries and
   purchase tests must use a Play-installed build and a configured license tester;
   Expo Go and a directly installed debug APK are not sufficient.

Set these backend secrets/variables:

```text
GOOGLE_PLAY_PACKAGE_NAME=com.folin.app
GOOGLE_PLAY_PRO_PRODUCT_ID=<the immutable subscription product ID>
GOOGLE_PLAY_PRO_BASE_PLAN_ID=<the active base-plan ID>
GOOGLE_PLAY_SERVICE_ACCOUNT_JSON=<one-line JSON or base64-encoded JSON>
```

## Real-time Developer Notifications

1. Create a Google Cloud Pub/Sub topic and connect it in Play Console's
   monetization setup.
2. Allow Google's Play notification service identity to publish to the topic.
3. Create an authenticated push subscription targeting:

   `https://api.folin.in/api/payments/google-play/rtdn`

4. Configure a dedicated service account as the push identity and set the exact
   endpoint URL as the OIDC audience.
5. Set:

```text
GOOGLE_PLAY_RTDN_AUDIENCE=https://api.folin.in/api/payments/google-play/rtdn
GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_EMAIL=<authenticated push service-account email>
```

Send Play Console's test notification after deployment. Folin validates the push
JWT, de-duplicates Pub/Sub message IDs, re-queries Google instead of trusting the
notification body, and then reconciles the entitlement.

## Expected behavior

- New purchase: verify, acknowledge, and activate Pro.
- Renewal: extend the stored expiry.
- Cancellation: stop auto-renewal but retain Pro through the paid expiry.
- Grace period: retain Pro while Google retries payment.
- Account hold, pause, or expiry: remove Pro.
- Refund/revocation: remove Pro immediately when Google reports revocation.
- App foreground: re-check the latest known purchase as recovery if a push was missed.
- Restore purchases: query Play, verify the token again, and bind it only to the
  Folin account that initiated checkout.
- Account deletion: stop the next Play renewal before deleting Folin's account
  binding and purchase token; this cancellation does not issue a refund.

Refund approval remains manual at launch: find the `GPA.` order in **Play Console →
Order management** and choose refund-only or refund-and-revoke. The app's policy
does not promise automatic refunds merely because a user asks.
