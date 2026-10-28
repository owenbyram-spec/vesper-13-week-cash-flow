# Set up the embedded assistant

This is an optional connection in the local browser tool. The default CLI needs no model setup and makes no provider requests. The browser assistant sends your prompt and selected workflow evidence to OpenAI when you explicitly ask it to use the real model. The model does not run on your computer. Synthetic rehearsal is clearly labelled and makes no model call.

1. Use your organization's approved OpenAI API account. Open the [API dashboard](https://platform.openai.com/) and its [project API keys](https://platform.openai.com/api-keys) page.
2. Create a project key with the permissions needed for Responses requests. Review your organization's data-use policy and the provider's current terms before sending client information.
3. Enable API billing if your account requires it. API usage is billed by the provider to your account. Review [current pricing](https://developers.openai.com/api/docs/pricing), usage and available [spending controls](https://developers.openai.com/api/docs/guides/production-best-practices); do not assume an alert is an enforced cap.
4. Open Connections & setup. Confirm its entity, local address and data directory match your intended app copy. Enter the key in the dedicated masked field, choose an available model and acknowledge the external data flow. Do not paste credentials into the assistant conversation or development chat. Closing the form or changing entity clears unsaved fields.
5. Choose session-only storage or persistence in the operating system credential store. If that store is unavailable, use session-only; the app never falls back to saving plaintext secrets.
6. Click **Save AI connection**. The prominent saved-connection status must change from “No AI connection saved” to “AI connection saved for” the intended entity (`configured: true` in detailed status). Saving alone does not test model access. Click **Test AI connection** next. This small billable API request sends only a generic connection prompt. A successful test in another copy or entity does not configure this one. If setup fails, share only the exact status/error message with your support contact; never share the key. Closing or reopening settings clears old feedback so it cannot serve as proof of a current test.
7. Close settings, turn **Synthetic rehearsal off**, then ask a scoped question about the current forecast. The assistant reads the app's tools and can draft reviewable proposals; you accept changes separately.

Save errors appear beside the AI controls. Missing required fields are identified before anything is sent. A rejected or unconfirmed Save keeps the entry masked in the open form so you can correct the error; closing the form, changing entity or choosing Remove clears that draft. A successful Save clears the key field only after the save response and a separate status read both confirm the connection. Test stays unavailable while saving, after a failed Save or while you have unsaved changes, so it cannot hide the original Save error.

## What each check establishes

- **Synthetic rehearsal:** actual local scoped tools plus scripted narration, labelled `synthetic-rehearsal`; zero model requests. It exercises the workflow without credentials.
- **Connection test:** a real generic request to the selected model. It checks that the app can reach that model using this entity's connection; it does not read financial tools or prove a scoped explanation works.
- **Real scoped assistant response:** rehearsal is off, the response is labelled `model`, and evidence cards refer to the displayed entity/version and its deterministic tool results. Compare those cards with the saved forecast. Asking for an explanation must leave the version and input/result hashes unchanged.

For a first real request, use the fictional entity and ask: “Use forecast_summary and forecast_coverage to explain the lowest projected cash point and source coverage findings. Do not draft or change assumptions.” Record the result separately from the connection test. If only the test succeeds, the scoped-response gate remains open. [Official function-calling guidance](https://developers.openai.com/api/docs/guides/function-calling) describes the model/tool/result exchange; this app bounds the tools to the active forecast.

The initial configurable model is `gpt-5.6-terra`, documented in the [official model reference](https://developers.openai.com/api/docs/models/gpt-5.6-terra). Availability depends on your account. Enter another compatible Responses/function-calling model if needed; there is no silent provider fallback. No real model has been validated without separately authorized credentials.

Replace/remove a key through settings. Revoke it through the provider's dashboard if compromised. A missing/expired key or provider outage preserves your saved financial work.

[Function calling](https://developers.openai.com/api/docs/guides/function-calling) and [API key security](https://developers.openai.com/api/reference/overview) are the implementation references. Keys are used by the local backend and are never returned to browser storage, exports or logs.
