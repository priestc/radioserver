# AI Backends for Date Finder

RadioServer's `ai_date_finder` command can use several AI backends to look up track release years. This page covers how to get an API key for each, how to install it, and what the free tier looks like.

## Usage

```bash
radioserver ai_date_finder 6297 --backend <backend_name> --dry-run
```

---

## Claude (Anthropic)

**Default backend.** Anthropic's Claude models are known for accuracy and reliability.

- **Get a key:** [console.anthropic.com](https://console.anthropic.com/) — sign up and create an API key under Settings > API Keys
- **Model used:** `claude-haiku-4-5` (fast and cheap)
- **Free tier:** $5 in free credits for new accounts. Pay-as-you-go after that. Haiku is very affordable at fractions of a cent per request.

```bash
radioserver install_anthropic sk-ant-your-key-here
radioserver ai_date_finder 6297 --backend claude
```

---

## Groq

Groq runs open-source models on custom LPU hardware, making it extremely fast.

- **Get a key:** [console.groq.com](https://console.groq.com/) — sign up (no credit card required) and create an API key
- **Model used:** `llama-3.3-70b-versatile`
- **Free tier:** Generous free tier with no credit card required. Rate limits vary by model but generally allow thousands of requests per day. Check your limits at console.groq.com/settings/limits.

```bash
radioserver install_groq gsk_your-key-here
radioserver ai_date_finder 6297 --backend groq
```

---

## DeepSeek

Chinese AI lab offering very capable models at extremely low prices.

- **Get a key:** [platform.deepseek.com](https://platform.deepseek.com/) — sign up and generate an API key
- **Model used:** `deepseek-chat`
- **Free tier:** New accounts receive 500M free tokens — more than enough for tens of thousands of track lookups. After that, pricing is among the cheapest available (under $0.03 per million input tokens).

```bash
radioserver install_deepseek sk-your-key-here
radioserver ai_date_finder 6297 --backend deepseek
```

---

## OpenAI

The most well-known AI API provider.

- **Get a key:** [platform.openai.com](https://platform.openai.com/) — sign up and create an API key under API Keys
- **Model used:** `gpt-4o-mini`
- **Free tier:** New accounts get $5 in free credits (expires after 3 months, no credit card needed). The free tier is limited to 3 requests per minute and only allows GPT-3.5 Turbo — `gpt-4o-mini` requires a paid account. Pay-as-you-go pricing is affordable for this use case.

```bash
radioserver install_openai sk-your-key-here
radioserver ai_date_finder 6297 --backend openai
```

---

## Google AI (Gemini)

Google's Gemini models accessed through AI Studio.

- **Get a key:** [aistudio.google.com](https://aistudio.google.com/) — sign in with your Google account and create an API key
- **Model used:** `gemini-2.0-flash`
- **Free tier:** No credit card required. 15 requests per minute, up to 1,000 requests per day for Flash models. Limits reset daily at midnight Pacific Time. Note: free tier availability may vary by region.

```bash
radioserver install_google_ai AIza-your-key-here
radioserver ai_date_finder 6297 --backend google
```

---

## Recommendation

For free usage, **Groq** or **DeepSeek** are the best options — both have generous free tiers and no credit card requirement. **Claude** is the default and recommended for accuracy if you have credits available.
