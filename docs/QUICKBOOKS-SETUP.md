# Connect QuickBooks Online

The Intuit app/grant instructions below also supply credentials for the current PE-native workspace. Follow [PE-native setup](PE-PLATFORM-SETUP.md) for that workspace's launcher, Connections screen and native acquisition/review controls. The local UI names, forecast timing and refresh behavior described below belong to the legacy browser; its financial snapshots are not converted automatically.

The optional **local browser tool** has a read-only QuickBooks Online connector. The default `forecast.py` CLI reads canonical JSON and has no QuickBooks connection command. No OpenAI key is needed for accounting acquisition. For initial local installation and the file-only route, start with [Accounting setup](ACCOUNTING-SETUP.md).

This release uses **your own Intuit developer app** and an authorisation obtained through Intuit's OAuth Playground. It does not include a one-click OAuth callback, shared Vesper credentials or a hosted sign-in service. Live sandbox and production acquisition remain unverified; the steps below combine code inspection with official setup references checked on **2026-10-28**.

## 1. Prepare the local workspace

Start the optional browser tool in its own directory, for example:

```sh
python3.12 run.py --port 8880 --data-dir ./local-data/accounting-trial
```

Use a free port; on Windows replace `python3.12` with `py -3.12`. First launch installs pinned browser dependencies. Keep Terminal running. This creates a separate trial and leaves the existing 8876 app/data alone. Reuse this same directory to resume your own trial; a port change alone does not create separate data.

Create a real entity using **Create my entity** or **+ New entity → Entity name → Forecast currency → Create workspace**. Choose the source company's currency. QBO acquisition is refused in the labelled synthetic workspace. In **Connections & setup**, verify this entity, the local address and the resolved data directory.

## 2. Create an Intuit app and obtain a sandbox grant

You need an Intuit developer account and permission to connect the intended QuickBooks Online company. Start with a sandbox company rather than live client records.

1. Sign in to [Intuit Developer](https://developer.intuit.com/) and create/select an app for the **QuickBooks Online Accounting** API.
2. Open the app's **Development** credentials, labelled **Keys & OAuth** or **Keys & credentials** in the portal. Keep its Client ID and Client secret available for local entry. Use the matching sandbox company.
3. In the app's redirect-URI settings for this environment, register and save this exact Playground URI, preserving case and path:

```text
https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl
```

4. Open Intuit's [OAuth Playground](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0-playground), select your app/environment and the **Accounting** scope (`com.intuit.quickbooks.accounting`). Supply your own matching app credentials if requested. Payments and OpenID scopes are unnecessary for this connector.
5. Choose **Get Authorization Code**, sign in, select the intended sandbox company and authorise access.
6. In **Get Tokens from Authorization Code**, choose **Get Tokens**. Keep the returned **company ID / Realm ID** and **refresh token** for the local form. The app obtains access tokens itself; it has no access-token input field.

Intuit documents this sequence and redirect URI in its [Playground setup article](https://help.developer.intuit.com/s/article/Configuring-the-Oauth-2-0-Playground-for-QuickBooks-Online); its [credentials guide](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret) distinguishes Development and Production credentials. Portal labels may change. If authorisation fails, check the saved URI and environment before proceeding. The local app address, including ports 8766/8876/8880, is **not** this release's OAuth callback.

## 3. Save the connection in the intended entity

Return to **Connections & setup → 02 / QuickBooks Online (read only)**.

| Local field | Enter |
| --- | --- |
| Environment | **Sandbox first** for the development grant; **Production (eligible app required)** only for a production grant |
| Client ID | The ID of the Intuit app used to authorise this grant |
| Client secret | That same app/environment's secret |
| Realm ID | The numeric company ID returned by authorisation, not the developer app ID |
| Refresh token | The refresh token from that grant, not its access token or authorisation code |
| Store in OS credential store | Leave unchecked for process-memory storage, or explicitly select OS persistence |

Click **Save QBO connection** and check its result before continuing. Credentials belong to this local data directory **and** entity. Saving them in another copy or entity does not configure this one. Enter secrets only in the local masked fields and Intuit's official interface; never put them in chat, input JSON/CSV, GitHub issues or source files.

Unchecked storage lasts only while this backend process runs. Restarting requires re-entry/re-authorisation as needed; do not assume an old copied refresh token is still current. OS storage has no plaintext fallback, and its behaviour with actual credentials remains unverified. A locked/unavailable store must be resolved or session-only storage selected.

## 4. Test, inspect and explicitly bind the company

1. Click **Test company**. This makes a real token/company request; it is not a financial import.
2. Expand **Returned company — review before binding**. Compare the returned company name, Realm ID and environment with the intended client.
3. Only if they match, click **Confirm this company for the active entity**.
4. Check **Company confirmed for this entity** before refreshing financial records. If it is the wrong company, replace the connection using a grant for the correct company and test again.

These checkpoints are separate: **Save** stores connection details; **Test** checks access/company identity; **Confirm** binds that company to this workspace; **Refresh** acquires source records. A successful test alone does not establish a reviewed forecast or verified financial acquisition.

## 5. Acquire and review the first forecast

Click **Refresh current cash & open items**. Acquisition uses the current local date and reads:

- Account metadata and a dated accrual BalanceSheet report for bank-account opening cash, checking account coverage and parent/child totals.
- Complete open **Invoice** and **Bill** populations, using outstanding `Balance`, not original transaction totals.

The source currency must match the entity. Mixed currencies, unsupported precision, incomplete pages or missing bank coverage stop the refresh; no cash balance is invented. Multiple requests form an observation window, rather than an atomic accounting snapshot. Reconcile the cash anchor and open-item totals with QuickBooks reports before relying on them. Bank-account accounting balances do not automatically establish available cash or every cash equivalent.

Successful acquisition creates a new immutable **QuickBooks current-source refresh** version. New open items have source due dates but no reviewed expected cash date, so they do not yet schedule collections/payments. Open **Inputs & timing → Review & edit**, set **Expected cash** and **Timing basis**, and review missing dates and out-of-horizon items.

Add/review payroll, rent, taxes, financing and future-sales assumptions; mark coverage complete only after checking the evidence. Recurring amounts are the **full amount per occurrence**. Choose **Preview updated forecast**, review the differences and click **Save this reviewed version**. Scenario proposals use the separate **Approve exact changes as a new version** action. Then compare versions and reconcile selected-version exports. The [data-format guide](DATA-FORMAT.md) explains date boundaries and coverage.

This connector does not reconstruct historical open balances from today's data. Use previously captured immutable versions or dated, reviewed file snapshots for history.

## 6. Refresh, resume and disconnect

Manual refresh is sufficient for a first workflow. Optional **Refresh every 15 minutes while this app runs** works only while the local backend is running. Check the last successful refresh and coverage each time; a timer does not establish completeness.

Each successful refresh replaces source AR/AP rows, retains recurring assumptions and one-time `category=assumption` rows, and carries reviewed cash timing forward for matching source IDs. Payroll, rent, taxes, financing and future-sales coverage return to **missing** for renewed review. Retained assumptions dated on/before the new opening cash require review and are omitted to avoid double counting. Failed/partial acquisitions preserve the prior valid version.

The app saves the rotated access/refresh pair after token refresh in the selected storage mode. Avoid using the same grant concurrently in several app copies or continuing to refresh it in the Playground after connecting here; competing rotation can leave a copy with obsolete credentials. Reauthorise through Intuit when the grant expires or is revoked. See Intuit's [OAuth lifecycle documentation](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0).

**Disconnect & stop refresh** stops local polling and removes this entity's local credentials; imported versions remain. To revoke provider access as well, remove the connected app through QuickBooks/Intuit. A finance backup does not carry credentials to another directory or machine.

## 7. Move to a live company only when eligible

Use the app's **Production** Client ID/secret and a new production-company grant; sandbox credentials/data cannot be substituted. Complete Intuit's current production app details, assessment and account/company eligibility requirements. The [production guide](https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app) and [assessment FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ) are the authorities for your app's requirements. Do not submit invented URLs, privacy statements or compliance answers to bypass those gates.

Use the official Playground redirect for the grant workflow above where your production app permits it. This release does not host a production callback or create a tunnel. If Intuit does not permit the grant for your app/company, use reviewed file inputs until the eligibility issue is resolved. Production authorisation and acquisition have **not** been verified in this project.

## When a step fails

| Message or symptom | Check next |
| --- | --- |
| Set up a QBO connection in this workspace first | Verify entity, address and data directory; save in that exact workspace. Session-only credentials disappear after backend restart. |
| Test/confirm required | Complete **Test company**, inspect the returned identity, then **Confirm this company for the active entity**. |
| Invalid redirect during authorisation | Check the Playground URI is saved for the matching environment and exactly matches the request. |
| Grant could not be refreshed | Check app/environment/company pairing; obtain a fresh grant in Intuit's official interface and reconnect. Do not share tokens to debug it. |
| Company/workspace changed | Reopen setup in the intended entity and repeat Test/Confirm. |
| Source currency differs | Create/select an entity with the matching source currency; this build performs no conversion. |
| Missing bank-account coverage or incomplete read | Review source accounts/reports and the visible error; prior data is preserved. A dated CSV cash snapshot is an alternative. |
| Rate limited | Wait and retry; disable unnecessary polling rather than repeatedly clicking Refresh. |
| Cash receipts/payments missing after import | Review **Expected cash**, **Timing basis**, coverage and forecast boundaries. Due dates alone are not scheduled cash. |

Only read operations are implemented. Intuit's Accounting permission may still be broader than this tool's operations. Live acquisition, actual credential-store behaviour and Windows execution remain open gates in [Verification](VERIFICATION.md).
