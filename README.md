# Microsoft Entra Verified ID Demo

**Author:** Kevin Tigges<br>
**Date:** September 22, 2026

> **Disclaimer:** This is an independent proof-of-functionality demo. It is not authorized, endorsed, or supported by Microsoft, and Microsoft is not responsible for its use or any resulting loss or damage. Run it only in an isolated test tenant, never in production, and use it entirely at your own risk.

## Purpose

This demo was built to show how Microsoft Entra Verified ID can support employee onboarding and identity verification without retaining a copied identity document as the long-term credential.

It demonstrates three uses for one employee credential:

- **Onboarding:** verify the employee and create a Temporary Access Pass (TAP).
- **Help desk:** verify a caller without changing the account or creating a TAP.
- **Face Check:** compare a live Authenticator capture with the trusted photo embedded in the credential.

## What It Does

1. Reads pre-created Entra users whose Office location is `VIDDEMO`.
2. Sends a 15-minute issuance link to the user's first `otherMails` address.
3. Accepts an approved JPEG and embeds it in a custom `VerifiedEmployeeCard`.
4. Waits for issuance confirmation and operator approval.
5. Enables the linked Entra account when needed.
6. Verifies the issuer, employee reference, work email, and optional Face Check result.

The generated `EMP-XXXXXXXX` value is a demo credential reference, not an HR employee number or government identifier. The credential is still cryptographically signed by the configured Entra Verified ID issuer.

## How It Works

The live demo reads existing in-scope Entra accounts; it does not create them. From there it can issue the employee credential, enable the selected account after approval, and verify the credential for onboarding, help-desk, or Face Check decisions.

See [Demo Flow, API Calls, and Configuration](DEMO_FLOW.md) for the flow diagram, every Microsoft Graph and Verified ID call, validation rules, local state, and configuration values.

## Accounts In Scope

The employee picker queries Microsoft Graph for existing Entra users whose `officeLocation` exactly matches `demoOfficeLocation` in `config.json` (`VIDDEMO` by default). This attribute is the demo's scope marker; it is not an authorization boundary or group membership check.

Each selected user must have `givenName`, `surname`, `jobTitle`, `department`, and at least one personal address in `otherMails`. The app reads the live directory account, stores its immutable Entra object ID in local workflow state, and can enable that same account after approval. It does not create users, add users to scope, or discover credentials issued by other applications.

## Trust And Provider Limits

This demo is a self-operated issuer. It trusts the Entra directory record, the operator, and the uploaded employee photo. Face Check proves that the live person resembles the credential photo; it does not prove how that photo or the identity attributes were originally established.

An external identity verification provider would add independent proofing before issuance, such as government-document validation, fraud checks, document portrait extraction, liveness, assurance evidence, consent, and recovery handling. This application would consume that verified evidence and bind it to the Entra user before issuing the employee credential. The later presentation and Face Check flows could remain mostly unchanged.

## Demo Limitations

- Operator pages have no authentication, authorization, CSRF protection, or rate limiting.
- Client-secret authentication and local JSON state are demo choices.
- Uploaded photos are processed in memory and are not retained by the app.
- Microsoft Graph accepting an email request does not guarantee delivery.
- TAP values are displayed in the browser.
- The app does not create or delete Entra users or update Entra profile photos.
- Local state does not support multiple instances, failover, audit, or durable recovery.
- Logging, monitoring, privacy review, managed secrets, and operational controls are incomplete.

Production would require authenticated operator access, least privilege, managed identity or certificates, managed secrets, durable shared state, auditing, approved delivery channels, and a documented identity-proofing policy.

## Requirements

- Python 3.11+
- Microsoft Entra test tenant with Verified ID configured
- App registration with `VerifiableCredential.Create.All`
- Graph application permissions: `User.Read.All`, `User.EnableDisableAccount.All`, `UserAuthenticationMethod.ReadWrite.All`, and `Mail.Send`
- Current Microsoft Authenticator
- Public HTTPS endpoint capable of forwarding callbacks to local port `8080`
- Face Check add-on or Microsoft Entra Suite for Face Check

## Configure

```bash
cp config.example.json config.json
cp credentials/VerifiedEmployeeCard-display.example.json credentials/VerifiedEmployeeCard-display.json
cp credentials/VerifiedEmployeeCard-rules.example.json credentials/VerifiedEmployeeCard-rules.json
```

Replace the `YOUR-...` values in `config.json`. Publish the local display and rules JSON as an Entra custom credential named `VerifiedEmployeeCard`, then add its issuer DID and manifest URL to `config.json`.

This demo uses `entraClientSecret` for simple local setup. Production deployments should authenticate with a certificate or managed identity instead of a client secret.

Face Check requires a newly issued credential with a `photo` claim mapped from input claim `photo` and displayed as `image/jpg;base64url`.

Never commit `config.json`, `.env`, `.callback_api_key`, or `.demo_onboarding_state.json`.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app
```

I used ngrok for easy external access during development:

```bash
ngrok http 8080
```

Any HTTPS tunnel, reverse proxy, or hosted deployment can be used instead. Set `publicBaseUrl` to its public HTTPS origin, restart Flask, and open `http://localhost:8080`.

## Testing the Demo

The tests mimic Microsoft Graph and Verified ID responses without connecting to Microsoft or changing real users.

Normal workflow state is stored in `.demo_onboarding_state.json` so requests, callbacks, approvals, and results survive Flask restarts. It does not store uploaded photos, wallet tokens, or Face Check footage.

Tests redirect that storage to a temporary file and delete it afterward, leaving the real state file untouched.

The tests cover:

- Graph user lookup, Office location filtering, paging, email, account enablement, and TAP creation
- Employee selection, required attributes, approval, and account-state transitions
- Single-use issuance and verification invitation handling
- JPEG validation, resizing, compression, and photo claim encoding
- Issuance and presentation callback correlation and employee claim matching
- Onboarding, help-desk, and Face Check outcomes and confidence thresholds
- Protection against unintended user deletion or TAP creation

Run them with:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## References

- [Microsoft Entra Verified ID](https://learn.microsoft.com/entra/verified-id/)
- [Issue a Verified ID credential](https://learn.microsoft.com/entra/verified-id/verifiable-credentials-configure-issuer)
- [Configure a Verified ID verifier](https://learn.microsoft.com/entra/verified-id/verifiable-credentials-configure-verifier)
- [Use Face Check with Verified ID](https://learn.microsoft.com/entra/verified-id/using-facecheck)
- [Configure Temporary Access Pass](https://learn.microsoft.com/entra/identity/authentication/howto-authentication-temporary-access-pass)