# 11 — Vision reader shortlist

**2026-10-01 · public provider research.** A subsequent narrow paid DeepSeek
probe is recorded in [doc 12](12-deepseek-speed-probe.md); the cross-provider
benchmark below remains unperformed.
Prices below are USD per million uncached input/output tokens, as currently
published. Image tokenization differs between models, so these are not
per-screenshot costs. Provider speed claims are not measurements on `cachy`.

## Initial comparison

| Model and provider | Input / output | Reason to test |
| --- | --- | --- |
| DeepSeek V4.1 Flash, direct `deepseek-flash` | $0.15 / $0.60 off-peak; $0.30 / $1.20 peak | Owner's working baseline; confirmed image support. [Pricing](https://api-docs.deepseek.com/quick_start/pricing/), [vision](https://api-docs.deepseek.com/guides/vision/). |
| MiMo V2.6 Flash, direct `mimo-v2.6-flash` | $0.14 / $0.28 | Current multimodal Flash, with thinking disable supported in the official example. [Model](https://mimo.mi.com/models/en-US/mimo-v2.6-flash), [pricing](https://mimo.mi.com/docs/en-US/price/pay-as-you-go). |
| Qwen3.5 Flash, OpenRouter `qwen/qwen3.5-flash-02-23` | $0.065 / $0.26 | Older inexpensive vision model; test whether adequate for narrow heartbeat judgments and screenshot text. [Listing](https://openrouter.ai/qwen/qwen3.5-flash-02-23). |
| Gemma 4 26B A4B, DeepInfra `google/gemma-4-26B-A4B-it` | $0.07 / $0.34 standard | Smaller multimodal candidate; test hosted latency rather than inferring speed from active parameter count. [Listing](https://deepinfra.com/google/gemma-4-26B-A4B-it). |

Reserves: [GLM-5.3-Flash on DeepInfra](https://deepinfra.com/zai-org/GLM-5.3-Flash)
currently lists promotional $0.075 / $0.25 (normal $0.15 / $0.50).
[Gemini 3.1 Flash-Lite on OpenRouter](https://openrouter.ai/google/gemini-3.1-flash-lite)
lists $0.25 / $1.50. Avoid starting with Gemini 2.5 Flash-Lite: its
[listing](https://openrouter.ai/google/gemini-2.5-flash-lite) announces retirement
on October 20. MiMo V2.5 also retires October 21 per its current pricing page.

## Recommendation and measurement

Use direct DeepSeek as the provisional reader because the owner already
reports satisfactory experience. Compare the four initial candidates before
locking a default. Keep ordinary image interpretation and heartbeat model
selection independently configurable: a cheap narrow judge may pass heartbeat
tests while failing general screenshot transcription.

Test paired images for loading/results/error transitions and individual
screens for small text, dialogs, disabled controls and unfamiliar icons.
Include a no-change case and readable/unreadable text examples with ground
truth. Use the same source captures and questions, randomize request order,
record provider/model and reasoning settings, and use short bounded outputs.
Disable thinking or choose minimum effort where the provider supports it.

Measure request-to-valid-answer latency (median and tail), accuracy, malformed
responses, usage and actual image cost. Include upload time and reasoning
tokens. For OpenRouter pin and record the provider during comparisons.
Run crop and full-window cases separately; reduced resolution can destroy
small UI text. DeepSeek's low-detail mode downscales to 512×512 according to
its vision guide, so it needs its own readability test.

Choose the fastest backend that meets the task's accuracy bar, rather than
the one reporting the highest output tokens/s. A short heartbeat answer spends
little time generating text; image processing and first-response latency may
dominate. The end-to-end three-arm benchmark remains in
[doc 10](10-heartbeat-direction.md).
