# Querying vault-engine from iOS over Tailscale (ISSUE-N)

Once the engine is running as a persistent service on your Mac (or PC)
and Tailscale is up on both that machine and your iPhone, an iOS
Shortcut can hit `POST /query` and surface results in any context that
accepts text: Share Sheet, Spotlight, voice via Siri, Lock Screen
widget, Action Button. This is the "phone has a path to the brain"
piece of the ISSUE-N bundle.

## Prerequisites

1. **Engine running on a host machine.**
   - macOS: `./scripts/install-launchd-service.sh --vault /path/to/vault --bind <tailnet-ip> --token <token>`
   - Windows: `.\scripts\install-windows-service.ps1 -VaultPath ... -BindAddr <tailnet-ip> -HttpToken <token>`
2. **Tailscale up on the host machine.** `tailscale status` should show
   an assigned 100.x.y.z address. Use the MagicDNS name
   (e.g. `mac.tail-xxxx.ts.net`) in the Shortcut rather than the raw IP
   so the URL keeps working when the IP rotates.
3. **Tailscale up on the iPhone.** Same tailnet. Verify by hitting
   `http://<host-tailnet-name>:7842/health` in Safari first; it should
   return `{"status":"ok","running":true}` with no auth.
4. **HS256 signing secret + JWT generated.** The engine's auth is
   HS256-signed JWT, NOT a raw pre-shared bearer (see
   `src/vault_engine/auth.py`). Two-step setup:

   ```bash
   # 1. Generate the secret (pass this to the install script as --token).
   #    Stored as VAULT_ENGINE_HTTP_TOKEN in the LaunchAgent/NSSM env.
   #    NEVER paste this raw value into the iOS Shortcut.
   uv run python -c "import secrets; print(secrets.token_urlsafe(32))"

   # 2. Sign a JWT with that secret. The 'exp' claim is REQUIRED.
   #    Pick a reasonable expiry (e.g. 1 year out for personal use).
   uv run python -c "import jwt, time; print(jwt.encode({'sub':'vault-engine','exp':int(time.time())+31536000}, '<secret-from-step-1>', algorithm='HS256'))"
   ```

   The signed JWT (step 2 output) is what the iOS Shortcut sends in the
   `Authorization: Bearer <jwt>` header. The secret (step 1) never
   leaves the host. Rotating the secret invalidates all JWTs signed
   with the old secret, so plan rotations accordingly.

## Building the Shortcut

Open the Shortcuts app on iPhone or iPad, tap `+` to create a new
Shortcut, and add these actions in order:

### 1. Ask for Input — the query

- **Action**: "Ask for Input"
- **Prompt**: "What are you researching?"
- **Input Type**: Text
- **Default Answer**: leave blank

This becomes `Provided Input` for downstream actions.

### 2. Dictionary — the JSON body

- **Action**: "Dictionary"
- Add two keys:
  - `q` (Text) → tap the value field, pick "Provided Input" from the
    magic-variable picker. This binds the query text.
  - `k` (Number) → `5` (or however many results you want by default)

### 3. Get Contents of URL — the request

- **Action**: "Get Contents of URL"
- **URL**: `https://<host-tailnet-name>:7842/query`
  - Replace `<host-tailnet-name>` with your host's MagicDNS name
    (e.g. `mac.tail-xxxx.ts.net`). The Mac LaunchAgent and Windows
    NSSM service both bind to your Tailscale IP when you pass the
    appropriate `--bind` / `-BindAddr` at install time.
  - HTTPS is recommended once you put `tailscale serve` or
    `tailscale funnel` in front. The default install binds plain HTTP;
    plain `http://` works inside the tailnet because Tailscale's
    wireguard tunnel is the encryption layer.
- Tap **"Show More"** to expand the action.
- **Method**: `POST`
- **Headers**:
  - `Authorization` → `Bearer <signed-jwt>` (paste the JWT from step 2
    above — NOT the raw secret stored in `VAULT_ENGINE_HTTP_TOKEN`).
  - `Content-Type` → `application/json`
- **Request Body**: select **"JSON"** as the body type, then drag the
  Dictionary from step 2 in as the body content.

### 4. Get Dictionary Value — extract hits

- **Action**: "Get Dictionary Value"
- **Get**: `Value for Key`
- **Key**: `fused_hits`
- **Dictionary**: the output of "Get Contents of URL" (auto-binds).

This pulls out the `fused_hits` array from the response shape:

```json
{
  "intent": "semantic",
  "fused_hits": [
    {"doc_id": "...", "rrf_score": 0.42, "channels": ["vector"], "per_channel_scores": {...}},
    ...
  ]
}
```

### 5. Repeat with Each — iterate hits

- **Action**: "Repeat with Each"
- **Input**: the `fused_hits` list from step 4.
- Inside the loop:
  1. **Get Dictionary Value** → key `doc_id` from "Repeat Item".
  2. **Text** action → combine the `doc_id` and any other fields you
     want shown. Example template:
     `[doc_id]` (where `doc_id` is the magic variable from the
     previous step).
  3. **Add to Variable** → variable name `results`. This accumulates
     a list of formatted hits.

End the loop.

### 6. Combine Text — flatten the results list

- **Action**: "Combine Text"
- **Combine**: variable `results`
- **With Separator**: New Lines

### 7. Show Result

- **Action**: "Show Result"
- **Text**: the output of "Combine Text".

`Show Result` works well in interactive mode. For Siri / Action Button
use, swap it for "Speak Text" or "Quick Look" depending on what you
want.

## Saving and triggering

- **Name**: "Ask Brain" (or similar)
- **Icon**: any. Glyph in the brain category is a fitting choice.
- **Add to Home Screen** or **Add to Lock Screen** if you want a tap
  shortcut.
- **"Use with Siri"**: tap the shortcut, then "i" → "Add to Siri". Now
  you can say "Hey Siri, Ask Brain" and dictate the query.
- **Share Sheet**: enable "Show in Share Sheet" in the shortcut's
  settings if you want to query the brain about selected text from
  Safari, Notes, Messages, etc. When invoked from the Share Sheet, the
  shared text replaces "Ask for Input" as `Provided Input`.

## Privacy and operational notes

- **JWT in clear inside the Shortcut.** iOS Shortcuts store action
  parameters as plain text in iCloud sync. The JWT is shorter-lived
  than the host secret — rotate by generating a new JWT (against the
  same host secret) and re-pasting it into the Shortcut. The
  underlying secret stays put on the host. If the JWT leaks, the
  blast radius is limited to "tailnet attackers can use it until the
  exp claim hits or the secret rotates" — not "attacker can forge
  arbitrary new tokens."
- **Tailscale tail-net only.** The default install binds to the
  tailnet IP. The engine refuses to bind to a non-loopback interface
  without a token; the install scripts enforce the same at install
  time so misconfig fails loudly, not silently.
- **`/health` is unauthenticated.** Useful for the "is the brain up?"
  check from the phone. Don't rely on the response body for trust
  decisions — it's a liveness probe, not an attestation.
- **No external API.** All requests stay on the tailnet between the
  phone and the host. The engine itself never reaches out.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Could not get the response` in the Shortcut | Host machine asleep, engine not running, or Tailscale down on either end. Try `ping <host-tailnet-name>` in a terminal app. |
| 401 Unauthorized + "jwt rejected" | Shortcut header is sending the raw secret, not a JWT signed with it. Re-do step 2 (the `jwt.encode` step) and paste the OUTPUT as the Bearer value. |
| 401 Unauthorized + "signature mismatch" | The JWT was signed with a different secret than the one in `VAULT_ENGINE_HTTP_TOKEN`. Regenerate the JWT against the current host secret. |
| 401 Unauthorized + "token expired" | The `exp` claim in your JWT has passed. Generate a new one with a future `exp`. |
| 422 Validation Error | Query too long (>2000 chars) or `k` outside 1-100. Trim the query or lower `k`. |
| Empty `fused_hits` | Engine running but no vault content matched. Check the host's logs (`~/Library/Logs/vault-retrieval-engine/vault-engine-stderr.log` on Mac). |
| Works on Wi-Fi at home but not on cell | Tailscale is up on phone but not actively routing — open the Tailscale app and verify the host is reachable. |
