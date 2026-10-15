# Story TTS API — Vietnamese chapters to audio

A small API service that lets a Vietnamese story-reading website turn a chapter into a playable MP3 on demand. It supports the Edge neural voices **Nam Minh** and **Hoài My**, with speeds **0.75×, 1×, 1.25×, 1.5× and 2×**.

## Integration flow

1. The reader presses Play on a chapter.
2. Your website's **server** obtains the chapter text, then calls \`POST /v1/tts\` with \`text\`, \`chapter_id\`, \`voice\` and \`rate\`.
3. The API queues synthesis and immediately returns a \`job_id\`.
4. Your server polls \`GET /v1/jobs/{job_id}\` until \`status\` is \`completed\` or \`failed\`.
5. When complete, the response includes a short-lived \`audio_url\` and \`download_url\`. Your page can pass the signed URL to an HTML audio player.

The API does not scrape chapter URLs or bypass login/paywalls. The reading site's backend sends the chapter content that the reader is authorized to access. **Never place \`TTS_API_KEY\` in browser JavaScript**: call this API from your website's server and give the browser only the temporary signed audio URL.

## Supported voices and speeds

| Name | Voice ID |
|---|---|
| Nam Minh | \`vi-VN-NamMinhNeural\` |
| Hoài My | \`vi-VN-HoaiMyNeural\` |

Supported speeds: \`0.75\`, \`1.0\`, \`1.25\`, \`1.5\`, \`2.0\`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | \`/health\` | Public health check |
| GET | \`/\` | Service info |
| GET | \`/v1/voices\` | Voices and speed options; API key required |
| POST | \`/v1/tts\` | Submit a chapter for synthesis; API key required |
| GET | \`/v1/jobs/{job_id}\` | Read job status and receive fresh signed URLs; API key required |
| GET | \`/v1/jobs/{job_id}/audio\` | Play or download MP3 using a signed URL |
| GET | \`/docs\` | Interactive API documentation |

All protected endpoints expect \`Authorization: Bearer YOUR_TTS_API_KEY\`. Configure \`TTS_API_KEY\` on the API server before use. If it is missing, protected requests fail closed with HTTP 503.

### Example: submit a chapter

\`\`\`bash
curl -X POST "$STORY_TTS_API_URL/v1/tts" \
  -H "Authorization: Bearer $TTS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "chapter_id": "book-42/chapter-12",
    "text": "Nội dung chương truyện cần đọc...",
    "voice": "vi-VN-NamMinhNeural",
    "rate": 1.25
  }'
\`\`\`

Typical response (\`202 Accepted\`):

\`\`\`json
{
  "job_id": 12,
  "chapter_id": "book-42/chapter-12",
  "status": "queued",
  "voice": "vi-VN-NamMinhNeural",
  "voice_name": "Nam Minh",
  "rate": 1.25,
  "cache_hit": false,
  "reused_request": false,
  "status_url": "https://your-tts-api.example/v1/jobs/12",
  "audio_url": null,
  "download_url": null,
  "error": null
}
\`\`\`

When \`status\` becomes \`completed\`, \`audio_url\` can be used as the source of \`<audio controls>\`. Temporary signed URLs expire after 24 hours; poll job status again to receive a fresh URL.

### Example: integration from a Node.js website backend

This code must run on the server (Node 18+), not in browser JavaScript. Keep both environment variables in your website's server-side configuration.

\`\`\`js
const apiBase = process.env.STORY_TTS_API_URL;
const apiKey = process.env.TTS_API_KEY;

export async function requestChapterAudio({ text, chapterId, voice, rate }) {
  const response = await fetch(\`\${apiBase}/v1/tts\`, {
    method: "POST",
    headers: {
      "Authorization": \`Bearer \${apiKey}\`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      text,
      chapter_id: chapterId,
      voice: voice ?? "vi-VN-NamMinhNeural",
      rate: rate ?? 1.0
    })
  });
  if (!response.ok) throw new Error(\`TTS request failed: \${response.status}\`);
  return response.json();
}

export async function getChapterAudioJob(jobId) {
  const response = await fetch(\`\${apiBase}/v1/jobs/\${jobId}\`, {
    headers: { "Authorization": \`Bearer \${apiKey}\` }
  });
  if (!response.ok) throw new Error(\`TTS status failed: \${response.status}\`);
  return response.json();
}
\`\`\`

## Run locally

Requires Python 3.11+ and internet access for the Edge TTS service.

\`\`\`bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
python -m pip install -r requirements-dev.txt
\`\`\`

Set environment variables and launch:

\`\`\`powershell
$env:TTS_API_KEY = "replace-with-a-long-random-secret"
$env:AUDIO_URL_SECRET = "another-long-random-secret"
$env:ALLOWED_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
python main.py
\`\`\`

Open \`http://127.0.0.1:8000/docs\`.

## Deploy on Render

This repository includes \`render.yaml\`. In Render choose **New → Blueprint**, connect this repo, and review the resources before deployment. The blueprint provides a persistent disk for SQLite job state and MP3 cache, generates \`TTS_API_KEY\` and \`AUDIO_URL_SECRET\`, and asks you to configure \`ALLOWED_ORIGINS\` to the exact origin(s) of your website (comma-separated, no trailing slash). After deployment:

- Copy the service's base URL to \`STORY_TTS_API_URL\` on your reader site's server.
- Store the same generated \`TTS_API_KEY\` as a server-side secret on the reader site's backend.
- Never expose the API key in the browser or commit it to Git.

The blueprint uses a persistent disk and a paid-style service plan; check current Render pricing before creating it. This application intentionally runs as **one server worker** because its queue is in-process and state lives in SQLite on a single disk.

## Cache, queue, and limits

- Completed MP3s are cached by exact chapter text + voice + speed. Changing any of these produces a different cache key.
- Identical pending requests share the existing job, preventing duplicate work for the same chapter and options.
- Jobs are synthesized one at a time to reduce pressure on the upstream TTS service.
- The API does not split a chapter into separate MP3 files and has no application-level character-count cap. Edge-TTS handles protocol-level fragments internally; the upstream service may still reject unusually long input.
- \`MAX_QUEUED_JOBS\` defaults to 50. When the queue is full, the API responds with HTTP 429 and \`Retry-After: 30\`.
- The service retries temporary synthesis errors, but cannot guarantee the external Edge TTS service will accept every request. A 429 can mean upstream rate limiting; a 403 can indicate a protocol change.
- Generated audio and SQLite state are stored under \`TTS_DATA_DIR\`, default \`./data\`. Use persistent storage in deployment and back up this directory if the cache is valuable.
- Signed audio URLs expire after 24 hours by default (\`AUDIO_URL_TTL_SECONDS\`). A fresh URL is returned when the authenticated website polls the job endpoint again.
- The API stores the chapter text only while waiting or synthesizing; it clears text from the job row once synthesis succeeds or fails. Do not log chapter text or secrets.
- This is a single-process starter service. For large traffic, use an external durable queue and object storage rather than adding more web workers.

## Tests

\`\`\`bash
python -m pip install -r requirements-dev.txt
pytest -q
\`\`\`

GitHub Actions runs the suite on Python 3.11, 3.12 and 3.13. Tests mock the speech provider and therefore verify API contracts and caching without claiming that the external service is always available.

## License

MIT for this repository's code. Edge-TTS and other third-party components retain their own license terms.
