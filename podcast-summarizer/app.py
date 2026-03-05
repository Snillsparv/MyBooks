import os
import re
import json
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import anthropic
import urllib.parse
import urllib.request

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_video_title(video_id: str) -> str:
    """Fetch video title from YouTube oEmbed API."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("title", "Okänd titel")
    except Exception:
        return "Okänd titel"


def get_transcript(video_id: str) -> tuple[str, list[dict]]:
    """Fetch transcript for a YouTube video. Returns (full_text, segments)."""
    ytt_api = YouTubeTranscriptApi()
    transcript = ytt_api.fetch(video_id)

    segments = []
    for snippet in transcript:
        segments.append({
            "text": snippet.text,
            "start": snippet.start,
        })

    formatter = TextFormatter()
    full_text = formatter.format_transcript(transcript)
    return full_text, segments


SUMMARIZE_PROMPT = """\
Du är en expert på att sammanfatta podcast-avsnitt och videoinnehåll.

Givet följande transkription, gör följande:

1. Ge en kort sammanfattning (2-3 meningar) av hela innehållet.
2. Dela upp innehållet i logiska kapitel/sektioner (5-15 stycken beroende på längd).
3. Varje kapitel ska ha:
   - En kort, beskrivande rubrik på svenska
   - En tidsperiod (t.ex. "0:00\u20135:30") baserat på textens position i transkriptionen
   - En sammanfattning på 1-3 meningar
   - Den faktiska transkriptionstexten för det avsnittet, organiserad under underrubriker. \
Inkludera de viktigaste delarna av transkriptionstexten \u2014 parafrasera och komprimera där det behövs, \
men behåll viktiga citat ordagrant. Formatera som HTML-fragment med <h4> för underrubriker och <p> för stycken.

Svara ENBART med giltig JSON i följande format (ingen markdown, inga kodblock):
{{
  "title": "Titel på avsnittet",
  "summary": "Övergripande sammanfattning...",
  "chapters": [
    {{
      "title": "Kapitelrubrik",
      "time": "0:00\u20135:30",
      "summary": "Sammanfattning av kapitlet...",
      "transcript_html": "<h4>Underrubrik</h4><p>Text från transkriptionen...</p>"
    }}
  ]
}}

Här är transkriptionen:

---
{transcript}
---

Totallängd på videon: cirka {duration} minuter."""


def estimate_duration(segments: list[dict]) -> int:
    """Estimate video duration in minutes from segments."""
    if not segments:
        return 0
    last = segments[-1]
    return int((last["start"] + 30) / 60)


def summarize_with_claude(transcript_text: str, duration_minutes: int):
    """Send transcript to Claude for summarization. Streams the response."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = SUMMARIZE_PROMPT.format(
        transcript=transcript_text[:100000],
        duration=duration_minutes,
    )

    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=32000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        full_response = ""
        for text in stream.text_stream:
            full_response += text
            yield text

    return full_response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "Ingen URL angiven"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Kunde inte identifiera YouTube-video-ID. Kontrollera att länken är korrekt."}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY är inte konfigurerad på servern."}), 500

    try:
        video_title = get_video_title(video_id)
    except Exception:
        video_title = "Okänd titel"

    try:
        transcript_text, segments = get_transcript(video_id)
    except Exception as e:
        return jsonify({"error": f"Kunde inte hämta transkription: {e}"}), 400

    duration = estimate_duration(segments)

    def generate():
        yield json.dumps({"type": "meta", "video_title": video_title, "video_id": video_id, "duration": duration}) + "\n"

        try:
            full_text = ""
            for chunk in summarize_with_claude(transcript_text, duration):
                full_text += chunk
                yield json.dumps({"type": "chunk", "text": chunk}) + "\n"

            yield json.dumps({"type": "done"}) + "\n"
        except anthropic.BadRequestError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"
        except anthropic.AuthenticationError:
            yield json.dumps({"type": "error", "error": "Ogiltig API-nyckel. Kontrollera ANTHROPIC_API_KEY."}) + "\n"
        except anthropic.APIError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"

    return Response(
        stream_with_context(generate()),
        content_type="application/x-ndjson",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
