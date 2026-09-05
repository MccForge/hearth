# Devpost submission draft — Hearth (Amazon Developer Hackathon 2026, Alexa+ track)

## Project name
Hearth

## Tagline
A daily voice check-in for someone living alone, with the family kept in the loop.

## Inspiration
Millions of older adults live alone. The daily worry for their families is simple and constant: are they okay this morning? Amazon built a product for this once, Alexa Together, and shut it down in 2023. The "tap to say I'm OK" apps that remain are silent buttons. What families actually want is what a good neighbor does: a short conversation, a kind ear, a reminder about the appointment, and a phone call to the daughter if something sounds wrong. Alexa+ can finally do that, and MCP is how it plugs into a system that remembers, scores, and escalates.

## What it does
Every morning, inside the check-in window, the assistant runs a two-minute conversation. If the daughter left a message, it plays first, in her own voice. Then: how are you feeling, how did you sleep, did you take your Lisinopril and Metformin, have you eaten, is anything bothering you, you've got Dr. Patel at two and Tom is driving you, Anna wanted to know whether the plumber came, what are your plans. Hearth records the interpreted answers and the exact words, flags anything a family member would never want missed (a fall, chest pain, dizziness, skipped medication, "help"), scores a 0–100 concern level, sends the family a one-paragraph summary with the answers and the week's trends, and escalates when the concern is high. If the check-in never happens, a watchdog climbs a ladder on its own: re-prompt at the window's end, notify the primary contact at +30 minutes, notify everyone at +90 with the last known status. "Not now" snoozes without nagging. "Tell Anna I love her" becomes a voice note in the dashboard. "I have the dentist on Friday at 10" becomes a reminder Hearth raises on Friday. "Call my daughter" alerts everyone instantly. When Anna is away, Tom is the go-to automatically, and Margaret is told so. A caregiver can ask "how is Mom today?" or "what's on Mom's calendar?" and get the answer. On a device with a screen the conversation has a face: a check-in card that ticks off each topic as she answers, shows her medication and the day's appointments, plays the family's message, turns amber when something is flagged and red when help is on the way. The family's phone gets the summary as a notification and a status card.

## How we built it
- **MCP server** (Python, official MCP SDK 2.x): fourteen tools, four resources, and a prompt over Streamable HTTP, MCP spec 2025-11-25, stateless and JSON-response so it sits behind any host. Structured results; family recordings returned as MCP audio content.
- **MCP Apps views** (spec 2026-01-26): three `ui://` views, check-in card, calendar, family status, served as `text/html;profile=mcp-app` resources and advertised by each tool through `_meta.ui.resourceUri`. Each is one self-contained HTML file with a forty-line postMessage bridge (`ui/initialize`, tool input and result notifications, `ui/message`, `size-changed`) and sizes itself from the host's container dimensions and safe-area insets. The same files render in Claude, ChatGPT, VS Code, or an Alexa+ device.
- **OAuth 2.1 shaped to the Alexa+ MCP Toolkit**: a service tier (`client_credentials`, HTTP Basic, `mcp:service`, resource-bound, 3600 s) for discovery, and a user tier (`authorization_code` + PKCE S256, `mcp:tools mcp:resources`, rotating refresh tokens) whose consent page is the account linking: the customer says whose home the device is in, and every token carries that person. Fixed client credentials, no DCR, metadata at both well-known paths, host allow-listing for the public tunnel. An end-to-end test walks the exact flow Alexa+ uses.
- **Agent Skill** (`skill/SKILL.md`) that gives the host the conversation order, tone, interpretation rules, and hard safety boundaries.
- **Domain logic**: transparent keyword flags with negation handling, concern scoring, summaries, seven-day trend insights, away-aware contact routing.
- **Escalation watchdog** with idempotent per-day ladder levels and an injectable clock.
- **Caregiver dashboard**: status, 14-day timeline with the actual words, alerts to acknowledge, trends, calendar, voice-message recorder with scheduling, questions queue, contacts with away mode, window settings.
- **AWS integration** (documented for the AWS Builder challenge): the simulator's host runs on Amazon Bedrock, Claude Sonnet 4.6 or Amazon Nova 2 Lite through the Converse API with a Bedrock API key, the same two model families Alexa+ itself uses; family email alerts go out through Amazon SES. Both are switched on by environment variables and off by default.
- **Alexa+ simulator**: an Echo Show style device and the family's phone in one page. It implements the host side of MCP Apps (sandboxed frames, initialize, tool input and results, `ui/message` back into the conversation, `tools/call` from a view), drives a scripted host with light language handling and no API dependency, or an LLM host over any OpenAI-compatible endpoint. Plays family audio, records voice notes back, shows every MCP call live. Speech in and out via the browser.
- **Certification self-check**: Amazon's inspector is preview-only, so `tests/certification_check.py` grades Hearth against the published functional requirements over the real endpoint and writes `certification-verdict.json` (22 rules, all passing): invocable tools, schemas, error shapes, stable ids, latency, continuity, views, metadata, OAuth.
- **Tests**: 19, covering parsers, negation, fresh-per-day records, context assembly, away routing, audio round-trip, events, ladder timing, snooze, the scripted host end to end, the MCP Apps surface over Streamable HTTP, and the two-tier OAuth flow.

## Challenges
Keeping the concern logic honest: keyword flags are transparent but naive, so negation handling ("I'm not hurt") and follow-up hints matter more than a cleverer model. Making a scripted host feel natural enough for a demo without an LLM: multi-fact answers, clarifying choices, repeats, off-script requests like "tell Anna" or "I have the dentist Friday". Keeping the escalation ladder idempotent across watchdog ticks, time zones, and a contact who is away. Designing the demo so judges see the real tool surface rather than a mock. And the Alexa+ MCP Toolkit turned out to be a private preview for select partners, so the simulator had to be faithful enough to stand in: it implements the MCP Apps host contract rather than faking the cards.

## Accomplishments
A complete loop: context → family voice → conversation → structured record → family summary with answers, reminders and trends → escalation → caregiver query, all through the same MCP tools a real host would call, verified over Streamable HTTP, with screens that follow the MCP Apps spec and an OAuth surface that follows the Alexa+ toolkit spec, demoable without hardware and without an API key.

## What we learned
Hosts don't need many tools; they need the right context up front and a follow-up hint after each answer. Families don't want a risk score, they want the words. And the feature that made testers stop and smile was the simplest: the daughter's real voice starting the morning.

## What's next
Real Alexa+ device testing when the toolkit opens up, an LLM host on Amazon Bedrock, email and SMS through AWS, multiple households per instance, a weekly family digest, a phone-call fallback when the device gets no answer.

## Built with
Python, MCP Python SDK 2.1 (Streamable HTTP, audio content, structured output), MCP Apps (ui:// views over postMessage), OAuth 2.1 with PKCE, Amazon Bedrock (Claude Sonnet 4.6 and Amazon Nova 2 Lite through the Converse API as the simulator's host, the same model families Alexa+ runs on), Amazon SES (family email), Starlette, Uvicorn, SQLite, vanilla HTML/JS, MediaRecorder and Web Speech APIs, pytest.

## Disclosure
Designed and directed by James McC; code, tests, and docs written with heavy use of Claude, reviewed and tested locally.

## Product feedback (required item)
- MCP Python SDK 2.x: the FastMCP → MCPServer rename broke every tutorial online; the error message that points to the migration guide saved an hour. `streamable_http_app()` mounted inside another Starlette app needs the session manager lifespan run manually; worth one line in the docs. Returning `AudioContent` from a tool works well and should be featured in the docs.
- Alexa+ track guidance: the MCP Toolkit docs read as self-serve but the program is a private preview, and the CLI's registry rejects accounts that aren't allow-listed; that cost us an afternoon of AWS setup before a forum reply confirmed participants can't call Alexa+. Saying so on the track page, and offering a reference host or sandbox that speaks MCP and renders MCP Apps, would remove the need for every team to build a simulator. A published example of account linking → person mapping, and a statement on whether hosts play MCP audio content, would help.
- MCP Apps: the spec is clear and the vanilla postMessage bridge fits in forty lines, but the `ui/message` content shape differs between the spec text (an object) and the SDK example (an array); hosts should accept both. `containerDimensions` and `safeAreaInsets` are the right way to size a view; viewport units inside sandboxed frames behave differently across embedders.
- Devpost: the "students only" filter should be a top-level facet; it's the first thing a non-student checks.

## Links
- Repo: https://github.com/MccForge/hearth
- Demo video: (to add)
