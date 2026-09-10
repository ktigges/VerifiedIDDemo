# Microsoft Entra Verified ID employee demo

This Flask application issues a reusable employee credential and demonstrates two relying-party flows:

- **Onboarding:** verify the credential, match it to the employee, and create a one-time Microsoft Entra Temporary Access Pass (TAP).
- **Help desk:** verify the same credential to confirm the caller without changing the account or creating a TAP.

The app can issue a credential to a new demo employee or reuse an existing issued demo credential. Password reset is a briefing scenario only; this sample does not reset passwords or authentication methods.

> Use an isolated demo tenant. Submitting the new-employee form creates a real cloud-only Entra member. Selecting an existing employee does not create another account.

## Customer-specific files

Only `.gitignore` should be committed among hidden paths. Runtime secrets/state, editor metadata, briefing exports, PowerPoint files, and `startngr.sh` are ignored.

Create local customer files from the checked-in examples:

```bash
cp config.example.json config.json
cp credentials/VerifiedEmployeeCard-display.example.json credentials/VerifiedEmployeeCard-display.json
cp credentials/VerifiedEmployeeCard-rules.example.json credentials/VerifiedEmployeeCard-rules.json
```

Modify these values before running or publishing a credential:

| File | Customer changes |
| --- | --- |
| `config.json` | Tenant/client IDs, client secret, issuer DID, manifest URL, accepted issuer DID, organization name, verified UPN domain, TAP lifetime, public HTTPS URL |
| `VerifiedEmployeeCard-display.json` | Organization name, card text, colors, labels, and optional HTTPS logo URL |
| `VerifiedEmployeeCard-rules.json` | Credential type, claim mapping, validity, and indexing policy if the customer's schema differs |

If `CredentialType` is renamed, change it in the rules, Entra credential contract, and `config.json`. Keep `email` and `employeeId` mappings unless the application claim-matching code is changed too.

`organizationName` controls the Flask page branding. Environment variable `ORGANIZATION_NAME` overrides it.

## What the app does

1. The operator enters employee attributes.
2. Microsoft Graph creates a cloud-only account in the configured verified domain.
3. The app requests `VerifiedEmployeeCard` issuance to Microsoft Authenticator.
4. The employee chooses onboarding or help-desk verification.
5. The app validates credential type, trusted issuer, `employeeId`, and `email`.
6. Onboarding creates a one-time TAP; help desk records only successful verification.

Local records persist in the ignored `.demo_onboarding_state.json` file so Flask reloads retain callback and cleanup IDs.

## Prerequisites

- Python 3.11+
- Microsoft Entra test tenant and verified UPN domain
- Current Microsoft Authenticator on a mobile device
- Public HTTPS endpoint forwarding to local port 8080
- **Authentication Policy Administrator** for Verified ID and TAP policy setup
- **Application Administrator** for app registration and consent
- Azure subscription/resource-group access for Verified ID Advanced Setup or Face Check

## Entra setup

### 1. Configure Verified ID

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com).
2. Open **Verified ID** and complete tenant setup.
3. Use Quick setup when available. Advanced Setup requires Azure Key Vault, DID registration, and domain verification; complete all portal checks.
4. Open **Credentials > Add a credential > Custom Credential**.
5. Name it exactly `VerifiedEmployeeCard` unless you also update every `CredentialType` reference.
6. Paste the customer's local `VerifiedEmployeeCard-display.json` into the display definition.
7. Paste the customer's local `VerifiedEmployeeCard-rules.json` into the rules definition.
8. If shown, enable **Publish credential to Verified ID network**, then create/save.
9. Open **Issue credential** and record the `authority` DID and complete `manifest` URL.
10. Record the directory/tenant ID under **Entra ID > Overview**.

The example contract maps `given_name`, `family_name`, `email`, `job_title`, `department`, and `employee_id`. Here, `employee_id` is a generated credential reference, not an HR employee number. The immutable Entra object ID stays only in server-side state.

### 2. Register the application

1. Go to **Entra ID > App registrations > New registration**.
2. Create a single-tenant registration. No redirect URI is required for client-credential authentication.
3. Add this **application** permission from **APIs my organization uses > Verifiable Credentials Service Request**:
   - `VerifiableCredential.Create.All`
4. Add these Microsoft Graph **application** permissions:
   - `User.Read.All` to resolve a pre-created user by exact UPN
   - `UserAuthenticationMethod.ReadWrite.All` for TAP creation
5. Grant tenant-wide admin consent for all permissions.
6. Create a client secret under **Certificates & secrets** and retain its **Value**, not its secret ID.
7. Record the application/client ID.

### 3. Enable Temporary Access Pass

1. Go to **Entra ID > Authentication methods > Policies > Temporary Access Pass**.
2. Enable TAP and include the pre-created users or onboarding group.
3. **All users** is simplest only in an isolated demo tenant.
4. Configure minimum, maximum, and default lifetimes and permit one-time use.
5. Set `tapLifetimeMinutes` inside that allowed range and save.

Production should use a controlled group. This sample does not change group membership or enable an account. A disabled account can receive the employment credential, but it must be enabled under the employer's policy before its TAP can be used to sign in.

## Microsoft Entra Verified ID Face Check

Face Check is first-party Microsoft functionality and does **not** require a third-party face-matching provider. Microsoft Authenticator captures a live selfie; Verified ID uses Azure AI Vision Face API for liveness and compares it to a trusted photo in the credential. The verifier receives a match-confidence result, not the selfie or liveness footage. Microsoft states the footage is discarded after processing.

A third-party provider is still relevant when policy requires independent government-ID/document proofing to establish that the credential's source photo belongs to the real-world person. Face Check proves live-person-to-credential-photo similarity; it does not establish how trustworthy the original photo was.

### Face Check Entra setup

1. Configure Verified ID first.
2. Use Microsoft Entra Suite, where Face Check is included, or enable the premium Face Check add-on.
3. Associate an Azure subscription with the Entra tenant.
4. Grant the setup operator **Contributor** on the subscription or resource group.
5. In **Verified ID > Overview > Add-ons**, enable Face Check.
6. Select subscription, resource group, and resource location; select **Validate**, then **Enable**.
7. Use Microsoft Authenticator on a supported, unmodified device. Microsoft currently documents Android API 26+ with Play Integrity strong integrity or supported iOS (iOS 11+).

### Activate Face Check for this app

The application code, example credential definitions, issuance form, presentation request, callback validation, and result page support Face Check. Complete these tenant steps before using the live branch:

1. Enable the Face Check add-on as described above.
2. Replace the existing Entra `VerifiedEmployeeCard` display and rules definitions with the updated local `credentials/VerifiedEmployeeCard-display.json` and `credentials/VerifiedEmployeeCard-rules.json` files, then save/publish the contract.
3. Confirm the saved contract contains a `photo` claim with display type `image/jpg;base64url` and a required, non-indexed `$.photo` input mapping.
4. Set `faceCheckMatchConfidenceThreshold` in `config.json` from 50-100. The default is 70.
5. Restart Flask after changing configuration.
6. Issue a new credential using an HR-approved JPEG. Previously issued credentials do not acquire the new photo claim and must be reissued.

The app validates JPEG format, a minimum size of 200 x 200 pixels, and a maximum file size of 1 MB. The 1 MB ceiling follows Microsoft's documented maximum photo size for a Verified ID used with Face Check; base64 encoding also enlarges the issuance payload. Resize or compress the portrait rather than uploading a full-resolution camera original. The app URL-encodes the base64 JPEG into the issuance request but does not save the photo in `.demo_onboarding_state.json`.

Microsoft also offers a no-code proof: issue its `VerifiedEmployee` credential from MyAccount and present it through the [public Face Check test app](https://aka.ms/vcempver).

## Local configuration

After copying `config.example.json`, replace every `YOUR-...` value:

| Setting | Purpose |
| --- | --- |
| `azTenantId` | Directory/tenant ID |
| `azClientId` | App registration client ID |
| `azClientSecret` | Client secret **Value** |
| `DidAuthority` | Issuer DID |
| `CredentialManifest` | Complete manifest URL |
| `CredentialType` | Credential contract type |
| `acceptedIssuers` | Trusted issuer DID(s), semicolon-separated |
| `organizationName` | Customer organization name |
| `clientName` / `purpose` | Wallet request text |
| `tapLifetimeMinutes` | TAP lifetime allowed by policy |
| `faceCheckMatchConfidenceThreshold` | Required Face Check score from 50-100; default `70` |
| `publicBaseUrl` | Public HTTPS origin without a path |

The callback is `{publicBaseUrl}/api/verifiedid/callback`. The app creates an ignored `.callback_api_key`; never reuse the Entra client secret for callbacks.

Never commit `config.json`, `.env`, `.callback_api_key`, or `.demo_onboarding_state.json`.

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app
```

In another terminal:

```bash
ngrok http 8080
```

Set `publicBaseUrl` to ngrok's HTTPS origin, restart Flask, and open that URL. A reserved domain can use ngrok's `--url` option.

## Run the two demo scenarios

### Scenario 1: issue and approve a Verified ID

1. HR/IT creates the Entra user and populates `givenName`, `surname`, `jobTitle`, and `department`.
2. Open `/`, expand **Scenario 1**, and enter the user's exact UPN.
3. The app reads the directory record and stores its object ID privately; it does not create or modify the user.
4. Select an HR-approved JPEG employee photo and create the issuance QR.
5. The candidate scans the QR or opens the same-device Authenticator link, reviews the credential, and accepts it without signing in to Entra.
6. Wait for the `issuance_successful` callback and the relying-party choices.

### Scenario 2: present and verify the ID

1. Open `/` through the public endpoint.
2. Choose a demo employee and select **Use existing credential**.
3. Choose standard onboarding, help desk, or **Start Face Check**.
4. Start verification immediately, or select **Create one-time link** for onboarding or help desk and send the resulting 15-minute URL to the employee.
5. Opening an invitation does not consume it. Selecting **Start verification** consumes it once and creates the short-lived Microsoft presentation request.
6. On another device, scan the QR. On the phone holding the credential, select **Open in Authenticator on this device** instead.
7. For Face Check, consent to sharing and complete the live capture in Authenticator.
8. The app requires matching `employeeId`/`email` claims and a Face Check score at or above the configured threshold. It shows the score but receives no selfie footage and creates no TAP.

This path creates neither an account nor another credential.

Standard onboarding still releases a TAP for use at [Security info](https://aka.ms/mysecurityinfo). Help desk and Face Check are verification-only branches.

## Account mapping

The demo never calls `POST /users`, enables an account, changes its profile, or deletes it. It resolves an exact UPN and stores the returned Entra object ID in `.demo_onboarding_state.json`. The object ID is not placed in the credential; presentation matches the generated credential reference and UPN back to that private mapping.

The employee picker does not enumerate Entra ID. It lists only credential-issued records retained by this demo. Automated tests use a temporary isolated catalog and mocked Graph client.

## Test

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Security boundaries

- Operator pages are unauthenticated. Production must authenticate and authorize the exact-UPN lookup and issuance workflow.
- One-time verification invitations expire after 15 minutes, store only a SHA-256 token hash, and are consumed only when the holder starts verification so link scanners do not burn them.
- Issuance QR and PIN appear together; production must bind issuance to the proofed session.
- TAP values appear in-browser; production needs an approved secure delivery channel.
- Prefer managed identity or certificate authentication where supported.
- Add CSRF protection, authentication, rate limits, expiry, auditing, shared state, and managed secrets before deployment.

## Microsoft references

- [Advanced Verified ID tenant setup](https://learn.microsoft.com/entra/verified-id/verifiable-credentials-configure-tenant)
- [Issue a Verified ID credential](https://learn.microsoft.com/entra/verified-id/verifiable-credentials-configure-issuer)
- [Configure a Verified ID verifier](https://learn.microsoft.com/entra/verified-id/verifiable-credentials-configure-verifier)
- [Use Face Check with Verified ID](https://learn.microsoft.com/entra/verified-id/using-facecheck)
- [Microsoft Graph: Get user](https://learn.microsoft.com/graph/api/user-get)
- [Configure Temporary Access Pass](https://learn.microsoft.com/entra/identity/authentication/howto-authentication-temporary-access-pass)
