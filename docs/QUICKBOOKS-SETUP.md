# QuickBooks Online: your own connection

This build imports posted accounting data through your own Intuit developer application. It has no Vesper shared secret or hosted authentication service. Setup is more involved than signing into QuickBooks; CSV remains available.

1. Sign in to the [Intuit Developer Portal](https://developer.intuit.com/) and create an Accounting application in your own approved developer account.
2. Start with development credentials and a sandbox company. Follow Intuit's [OAuth Playground instructions](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0-playground).
3. Register the exact Playground redirect URI: `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl`. Select the Accounting scope, authorize the intended company and obtain tokens in Intuit's official interface.
4. In this app's dedicated connection form enter environment, Client ID, Client Secret, company/Realm ID and refresh token. Choose session-only storage or OS credential storage. Never enter these into chat, GitHub or CSV files.
5. Test the connection. Review the company returned by Intuit, then explicitly confirm it for the current workspace. A successful token refresh alone does not select a client workspace.
6. Refresh manually. Optional polling runs only while the app process is running. Check source coverage and the last successful refresh before relying on results.
7. To disconnect, remove the connection locally. If you also want to revoke Intuit access, manage the app in your QuickBooks/Intuit connected-app settings.

For live companies, obtain production credentials and satisfy Intuit's current app/account/company eligibility and enrollment requirements. Sandbox credentials cannot authorize a live company. Intuit's [own local CLI](https://github.com/intuit/intuit-cli-for-quickbooks) distinguishes sandbox localhost from production HTTPS. This tool imports a user-authorized refresh token; it does not invent a production localhost callback or create a tunnel. A live production grant has not been verified in development.

Bank Rec uses an account-filtered [GeneralLedger report](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/report-entities/generalledger). Its known subaccount-hierarchy issue requires conservative rejection of ambiguous reports. Independent bank statement data is still required: unreviewed bank-feed items are not exposed through the Accounting API.

The forecast uses a dated BalanceSheet bank-cash anchor and complete open Invoice/Bill reads. Source due dates remain separate from expected cash timing. Current open balances do not reconstruct historical AR/AP. Multiple requests are an observation window, not an atomic vendor snapshot.

No accounting-system writes are implemented. The provider's Accounting permission can be broader than the operations this app uses. Failed/partial refreshes must preserve prior valid work and remain visible. Reconnect when authorization expires or is revoked.
