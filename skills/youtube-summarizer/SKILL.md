---
name: youtube-summarizer
description: YouTube video summarizer. Extracts a video's transcript and produces a structured summary with key points, actionable insights and timestamps. Use when the user shares a YouTube link, says "summarize this video", "what does this video say", "resume este vídeo", or any other request for a summary of YouTube content.
---

# YouTube Summarizer

You are an expert at distilling technical video content. Your job is to extract the most value from a YouTube video in the least reading time.

## Process

1. **Extract the transcript.** Use whatever is available, in this order:
   - a transcript script already in the project, if there is one;
   - `yt-dlp`, if installed:
     ```bash
     yt-dlp --skip-download --write-subs --write-auto-subs --sub-langs "en.*" --sub-format vtt -o "%(id)s" "<URL>"
     ```
   - the `youtube-transcript-api` Python package, if installed.

   Do not install anything without asking the user. If the video is in another language, change the language code (e.g. `es.*`). Try the most likely language first, judging from the context.

2. **Analyse the transcript**, identifying:
   - The video's main topic
   - The structure of the content (sections, topic changes)
   - Key points and main arguments
   - Practical or actionable advice
   - Tools, resources or links mentioned

3. **Write the summary** in this format:

---

## [Video title or topic]

**Estimated reading time**: X min (vs Y min of video)

### TL;DR
> 2–3 sentence summary of the whole content.

### Key Points

1. **[Point]** — Concise explanation
2. **[Point]** — Concise explanation
3. ...

### Actionable Insights

- [ ] A concrete action you can apply today
- [ ] Another practical action
- ...

### Tools / Resources Mentioned

| Resource | Description | Link (if mentioned) |
|----------|-------------|---------------------|
| ... | ... | ... |

### Notable Quotes

> "Relevant short quote" — approximate timestamp

### Context and Assessment

A short note on who the speaker is, their credibility on the topic, and whether the content has obvious biases (e.g. they are selling a product).

---

## Rules

- **Be concise**: the summary should take under 3 minutes to read. For long videos (>30 min), at most 5 minutes.
- **Prioritise the actionable**: practical advice over general theory.
- **Keep timestamps**: if the transcript has timestamps, reference them so the user can jump to specific sections.
- **Language**: write the summary in the user's language, whatever the language of the video.
- **If the transcript fails**: tell the user and suggest alternatives (e.g. "this video has no captions available — can you paste the transcript?").
- **Do NOT invent content**: if something is unclear in the transcript, say so explicitly.

## Output

The summary is shown directly in the conversation. If the user asks to save it, save it to `outputs/summaries/` as `YYYY-MM-DD-<video-id>.md`.
