# Troubleshooting

- **Python missing/too old:** install Python 3.12+ from python.org; reopen the terminal/launcher. No system Python is modified.
- **Package download failed:** check network/proxy access to PyPI and rerun. The private environment is reused; saved application data is separate.
- **Port already used:** close the other app copy or run `python run.py --port 8875` (choose a free port). The launcher never kills an unrelated process.
- **Browser session expired:** reopen the app's root address and retry. Keep the local backend running.
- **AI unavailable:** verify the key, approved model, API billing/usage limits and data consent in settings. Provider errors preserve work. Synthetic rehearsal is available only on a labelled synthetic workspace.
- **Credential storage unavailable:** choose session-only storage and re-enter credentials after restarting. There is no plaintext fallback.
- **QuickBooks mismatch:** distinguish sandbox/production credentials; review and confirm the returned company. Reconnect revoked/expired authorizations through Intuit. Failed or incomplete imports cannot become zero financial data.
- **Bank statement will not pass:** compare account, period, currency, source rows and opening/activity/closing controls. Resolve exceptions against independent sources; do not force a balancing plug.
- **Forecast timing looks wrong:** source due dates and expected cash dates differ. Review collections/payment timing and per-occurrence recurring amounts. Historical current-balance reconstructions are unsupported.

A Windows launcher is supplied; Windows execution remains unverified until tested on Windows. See VERIFICATION.md for the checks actually run on this build.
