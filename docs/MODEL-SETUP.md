# Set up the embedded assistant

The application runs locally. Its assistant sends your prompt and the selected workflow evidence to OpenAI when you explicitly ask it to use the real model. The model does not run on your computer. Synthetic rehearsal is clearly labelled and makes no model call.

1. Use your organization's approved OpenAI API account. Open the [API dashboard](https://platform.openai.com/) and its [project API keys](https://platform.openai.com/api-keys) page.
2. Create a project key with the permissions needed for Responses requests. Review your organization's data-use policy and the provider's current terms before sending client information.
3. Enable API billing if your account requires it. API usage is billed by the provider to your account. Review [current pricing](https://developers.openai.com/api/docs/pricing), usage and available [spending controls](https://developers.openai.com/api/docs/guides/production-best-practices); do not assume an alert is an enforced cap.
4. Open the app's connection/settings panel. Enter the key in the dedicated masked field, choose an available model and acknowledge the external data flow. Do not paste credentials into the assistant conversation.
5. Choose session-only storage or persistence in the operating system credential store. If that store is unavailable, use session-only; the app never falls back to saving plaintext secrets.
6. Save, then choose the connection test. This is a small billable API request. Development verification uses mocked responses and does not run that test against a real account.
7. Ask a question about the current workflow. The assistant reads the app's tools and creates reviewable proposals; you accept changes separately.

The initial configurable model is `gpt-5.6-terra`, documented in the [official model reference](https://developers.openai.com/api/docs/models/gpt-5.6-terra). Availability depends on your account. Enter another compatible Responses/function-calling model if needed; there is no silent provider fallback. No real model has been validated without separately authorized credentials.

Replace/remove a key through settings. Revoke it through the provider's dashboard if compromised. A missing/expired key or provider outage preserves your saved financial work.

[Function calling](https://developers.openai.com/api/docs/guides/function-calling) and [API key security](https://developers.openai.com/api/reference/overview) are the implementation references. Keys are used by the local backend and are never returned to browser storage, exports or logs.
