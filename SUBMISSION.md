# Devpost submission draft — Hearth (Amazon Developer Hackathon 2026, Alexa+ track)

## Project name
Hearth

## Tagline
A daily voice check-in for someone living alone, with the family kept in the loop.

## Inspiration
Millions of older adults live alone. The daily worry for their families is simple and constant: are they okay this morning? Amazon built a product for this once, Alexa Together, and shut it down in 2023. The "tap to say I'm OK" apps that remain are silent buttons. What families actually want is what a good neighbor does: a short conversation, a kind ear, a reminder about the appointment, and a phone call to the daughter if something sounds wrong. Alexa+ can finally do that, and MCP is how it plugs into a system that remembers, scores, and escalates.

## What it does
Every morning, inside the check-in window, the assistant runs a two-minute conversation. If the daughter left a message, it plays first, in her own voice. Then: how are you feeling, how did you sleep, did you take your Lisinopril and Metformin, have you eaten, is anything bothering you, you've got Dr. Patel at two and Tom is driving you, Anna wanted to know whether the plumber came, what are your plans. Hearth records the interpreted answers and the exact words, flags anything a family member would never want missed (a fall, chest pain, dizziness, skipped medication, "help"), scores a 0–100 concern level, sends the family a one-paragraph summary with the answers and the week's trends, and escalates when the concern is high. If the check-in never happens, a watchdog climbs a ladder on its own: re-prompt at the window's end, notify the primary contact at +30 minutes, notify everyone at +90 with the last known status. "Not now" snoozes without nagging. "Tell Anna I love her" becomes a voice note in the dashboard. "I have the dentist on Friday at 10" becomes a reminder Hearth raises on Friday. "Call my daughter" alerts everyone instantly. When Anna is away, Tom is the go-to automatically, and Margaret is told so. A caregiver can ask "how is Mom today?" or "what's on Mom's calendar?" and get the answer.

## How we built it
- **MCP server** (Python, official MCP SDK 2.x): fourteen tools, a resource, and a prompt over Streamable HTTP, MCP spec 2025-11-25, stateless and JSON-response so it sits behind any host. Family recordings are returned as MCP audio content.
- **Agent Skill** (`skill/SKILL.md`) that gives the host the conversation order, tone, interpretation rules, and hard safety boundaries.
- **Domain logic**: transparent keyword flags with negation handling, concern scoring, summaries, seven-day trend insights, away-aware contact routing.
- **Escalation watchdog** with idempotent per-day ladder levels and an injectable clock.
- **Caregiver dashboard**: status, 14-day timeline with the actual words, alerts to acknowledge, trends, calendar, voice-message recorder with scheduling, questions queue, contacts with away mode, window settings.
- **Alexa+ simulator**: a scripted host with light language handling and no API dependency, plus an optional LLM host over any OpenAI-compatible endpoint. Plays family audio, records voice notes back, shows every MCP call live. Speech in and out via the browser.
- **Tests**: 15, covering parsers, negation, fresh-per-day records, context assembly, away routing, audio round-trip, events, ladder timing, snooze, and the scripted host end to end.

## Challenges
Keeping the concern logic honest: keyword flags are transparent but naive, so negation handling ("I'm not hurt") and follow-up hints matter more than a cleverer model. Making a scripted host feel natural enough for a demo without an LLM: multi-fact answers, clarifying choices, repeats, off-script requests like "tell Anna" or "I have the dentist Friday". Keeping the escalation ladder idempotent across watchdog ticks, time zones, and a contact who is away. Designing the demo so judges see the real tool surface rather than a mock.

## Accomplishments
A complete loop: context → family voice → conversation → structured record → family summary with answers, reminders and trends → escalation → caregiver query, all through the same MCP tools a real host would call, verified with a real Streamable HTTP client, demoable without hardware and without an API key.

## What we learned
Hosts don't need many tools; they need the right context up front and a follow-up hint after each answer. Families don't want a risk score, they want the words. And the feature that made testers stop and smile was the simplest: the daughter's real voice starting the morning.

## What's next
Account linking so a device maps to a person, real Alexa+ device testing, an SMS provider, multiple households per instance, a weekly family digest, a phone-call fallback when the device gets no answer.

## Built with
Python, MCP Python SDK 2.1 (Streamable HTTP, audio content), Starlette, Uvicorn, SQLite, vanilla HTML/JS, MediaRecorder and Web Speech APIs, pytest.

## Disclosure
Designed and directed by James McC; code, tests, and docs written with heavy use of Claude, reviewed and tested locally.

## Product feedback (required item)
- MCP Python SDK 2.x: the FastMCP → MCPServer rename broke every tutorial online; the error message that points to the migration guide saved an hour. `streamable_http_app()` mounted inside another Starlette app needs the session manager lifespan run manually; worth one line in the docs. Returning `AudioContent` from a tool works well and should be featured in the docs.
- Alexa+ track guidance: a reference host or a sandbox that speaks MCP would remove the need for every team to build a simulator. A published example of account linking → person mapping, and a statement on whether hosts play MCP audio content, would help.
- Devpost: the "students only" filter should be a top-level facet; it's the first thing a non-student checks.

## Links
- Repo: https://github.com/MccForge/hearth
- Demo video: (to add)
