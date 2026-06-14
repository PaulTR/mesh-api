# MESH-API Integrations

Setup guides for the AI providers and communication integrations that MESH-API
can drive. For the project overview see [README.md](README.md); for core
settings see [CONFIGURATION.md](CONFIGURATION.md); for the full per-extension
reference see [EXTENSIONS.md](EXTENSIONS.md).

- [Home Assistant Integration](#home-assistant-integration)
- [LLM API Integration](#llm-api-integration)
- [Email Integration](#email-integration)
- [Discord Integration](#discord-integration-detailed-setup--permissions)
- [Twilio Integration](#twilio-integration)

---

## Home Assistant Integration

> **Home Assistant is now an extension.** Configure it in
> `extensions/home_assistant/config.json` or via the WebUI Extensions Manager.
> See [EXTENSIONS.md](EXTENSIONS.md) for details.

- **Enable:** Set `"enabled": true` in `extensions/home_assistant/config.json`.
- **Configure:** Set the `url`, `token`, `channel_index`, and `timeout` fields in the extension config.
- **Security (Optional):** Enable `"enable_pin": true` and set `"secure_pin"` in the extension config.
- **Routing:** Messages on the designated channel are forwarded to Home Assistant. When PIN mode is enabled, include your PIN in the format `PIN=XXXX your message`.

---

## LLM API Integration

Set `"ai_provider"` in [config.json](config.json) to one of the 12 supported providers, then fill in the corresponding API key / URL / model fields:

- **LM Studio:** `"ai_provider": "lmstudio"` — configure `lmstudio_url`, and optionally set `lmstudio_chat_model` / `lmstudio_embedding_model` if using multiple models.
- **OpenAI:** `"ai_provider": "openai"` — provide `openai_api_key` and choose a model (default `gpt-4.1-mini`).
- **Ollama:** `"ai_provider": "ollama"` — configure URL, model, and optional generation overrides via `ollama_options`.
- **Claude:** `"ai_provider": "claude"` — provide `claude_api_key` (default model `claude-sonnet-4-20250514`).
- **Gemini:** `"ai_provider": "gemini"` — provide `gemini_api_key` (default model `gemini-2.0-flash`).
- **Grok:** `"ai_provider": "grok"` — provide `grok_api_key` (default model `grok-3`).
- **OpenRouter:** `"ai_provider": "openrouter"` — provide `openrouter_api_key` (default model `openai/gpt-4.1-mini`).
- **Groq:** `"ai_provider": "groq"` — provide `groq_api_key` (default model `llama-3.3-70b-versatile`).
- **DeepSeek:** `"ai_provider": "deepseek"` — provide `deepseek_api_key` (default model `deepseek-chat`).
- **Mistral:** `"ai_provider": "mistral"` — provide `mistral_api_key` (default model `mistral-small-latest`).
- **OpenAI-Compatible:** `"ai_provider": "openai_compatible"` — provide `openai_compatible_url`, `openai_compatible_api_key`, and `openai_compatible_model` for any provider with an OpenAI-compatible API.

All providers have a configurable `_timeout` (default 60 seconds).

> **Tip:** You can also define multiple **named AI endpoints** and assign a
> different one per channel from the **🔌 Manage AI Endpoints** panel inside the
> **🤖 Channel Agents** modal. Each endpoint has a live **heartbeat** status dot
> (token-free `/models` ping). See the **v0.7.3.1** and **v0.7.3.6** entries in
> the [CHANGELOG](CHANGELOG.md).

---

## Email Integration

- **Enable Email Alerts:**
  - Set `"enable_smtp": true` in `config.json`.
- **Configure SMTP:**
  - Provide the following settings in `config.json`:
    - `"smtp_host"` (e.g., `smtp.gmail.com`)
    - `"smtp_port"` (use `465` for SSL or another port for TLS)
    - `"smtp_user"` (your email address)
    - `"smtp_pass"` (your email password or app-specific password)
    - `"alert_email_to"` (recipient email address or list of addresses)
- **Behavior:**
  - Emergency emails include a clickable Google Maps link (generated from available GPS data) so recipients can quickly view the sender's location.
- **Note:**
  - Ensure your SMTP settings are allowed by your email provider (for example, Gmail may require an app password and proper security settings).

---

## Discord Integration: Detailed Setup & Permissions

> **Discord is now an extension.** Configure it in `extensions/discord/config.json` or via the WebUI Extensions Manager. The setup steps below for creating a Discord bot and permissions still apply.

![483177250_1671387500130340_6790017825443843758_n](https://github.com/user-attachments/assets/0042b7a9-8ec9-4492-8668-25ac977a74cd)


### 1. Create a Discord Bot
- **Access the Developer Portal:**
  Go to the [Discord Developer Portal](https://discord.com/developers/applications) and sign in with your Discord account.
- **Create a New Application:**
  Click on "New Application," give it a name (e.g., *MESH-API Bot*), and confirm.
- **Add a Bot to Your Application:**
  - Select your application, then navigate to the **Bot** tab on the left sidebar.
  - Click on **"Add Bot"** and confirm by clicking **"Yes, do it!"**
  - Customize your bot's username and icon if desired.

### 2. Set Up Bot Permissions
- **Required Permissions:**
  Your bot needs a few basic permissions to function correctly:
  - **View Channels:** So it can see messages in the designated channels.
  - **Send Messages:** To post responses and emergency alerts.
  - **Read Message History:** For polling messages from a channel (if polling is enabled).
  - **Manage Messages (Optional):** If you want the bot to delete or manage messages.
- **Permission Calculator:**
  Use a tool like [Discord Permissions Calculator](https://discordapi.com/permissions.html) to generate the correct permission integer.
  For minimal functionality, a permission integer of **3072** (which covers "Send Messages," "View Channels," and "Read Message History") is often sufficient.

### 3. Invite the Bot to Your Server
- **Generate an Invite Link:**
  Replace `YOUR_CLIENT_ID` with your bot's client ID (found in the **General Information** tab) in the following URL:
  ```url
  https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=3072&scope=bot
  ```
- **Invite the Bot:**
  Open the link in your browser, select the server where you want to add the bot, and authorize it. Make sure you have the "Manage Server" permission in that server.

### 4. Configure Bot Credentials in Extension Config
Update `extensions/discord/config.json` with the following keys (or use the WebUI Extensions Manager):
```json
{
  "enabled": true,
  "webhook_url": "YOUR_DISCORD_WEBHOOK_URL",
  "receive_enabled": true,
  "bot_token": "YOUR_BOT_TOKEN",
  "channel_id": "YOUR_CHANNEL_ID",
  "inbound_channel_index": 1,
  "send_ai": true,
  "send_emergency": true
}
```
- **webhook_url:**
  Create a webhook in your desired Discord channel (Channel Settings → Integrations → Webhooks) and copy its URL.
- **bot_token & channel_id:**
  Copy your bot's token from the Developer Portal and enable message polling by specifying the channel ID where the bot should read messages.
  To get a channel ID, enable Developer Mode in Discord (User Settings → Advanced → Developer Mode) then right-click the channel and select "Copy ID."

### 5. Polling Integration (Optional)
- **Enable Message Polling:**
  Set `"receive_enabled": true` in the extension config to allow the bot to poll for new messages.
- **Routing:**
  The `"inbound_channel_index"` key determines the mesh channel used by MESH-API for routing incoming Discord messages.

### 6. Testing Your Discord Setup
- **Restart MESH-API** (or hot-reload via the WebUI Extensions Manager).
- **Check Bot Activity:**
  Verify that the bot is present in your server, that it can see messages in the designated channel, and that it can send responses.
- **Emergency Alerts & AI Responses:**
  Confirm that emergency alerts and AI responses are being posted in Discord as per your extension configuration.

### 7. Troubleshooting Tips
- **Permissions Issues:**
  If the bot isn't responding or reading messages, double-check that its role on your server has the required permissions.
- **Channel IDs & Webhook URLs:**
  Verify that you've copied the correct channel IDs and webhook URLs (ensure no extra spaces or formatting issues).
- **Bot Token Security:**
  Keep your bot token secure. If it gets compromised, regenerate it immediately from the Developer Portal.

---

## Twilio Integration
- **Enable Twilio:**
  - Set `"enable_twilio": true` in `config.json`.
- **Configure Twilio Credentials:**
  - Provide your Twilio settings in `config.json`:
    - `"twilio_sid": "YOUR_TWILIO_SID"`
    - `"twilio_auth_token": "YOUR_TWILIO_AUTH_TOKEN"`
    - `"twilio_from_number": "YOUR_TWILIO_PHONE_NUMBER"`
    - `"alert_phone_number": "DESTINATION_PHONE_NUMBER"` (the number to receive emergency SMS)
- **Usage:**
  - When an emergency is triggered, the bot sends an SMS containing the alert message (with a Google Maps link if GPS data is available).
- **Tip:**
  - Follow [Twilio's setup guide](https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account) to obtain your SID and Auth Token, and ensure that your phone numbers are verified.
