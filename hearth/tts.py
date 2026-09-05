"""Optional Amazon Polly narration for the demo (HEARTH_TTS=polly). Synthesized lines are cached on disk by voice and text.

The AWS CLI does the talking, so no AWS SDK is needed in the app and no credentials pass through it: it uses the CLI's own
profile (HEARTH_AWS_PROFILE). On Windows, when the CLI lives inside WSL, set HEARTH_AWS_CLI="wsl.exe -d Ubuntu -- aws"."""
from __future__ import annotations
import hashlib, os, shlex, subprocess

VOICES = {"hearth": "Ruth", "margaret": "Amy", "anna": "Danielle"}
CACHE = os.environ.get("HEARTH_TTS_CACHE") or os.path.join(os.environ.get("HEARTH_MEDIA") or os.path.join(os.getcwd(), "data", "media"), "tts")


def enabled() -> bool:
    return os.environ.get("HEARTH_TTS", "").lower() == "polly"


def _cli() -> list[str]:
    return shlex.split(os.environ.get("HEARTH_AWS_CLI") or "aws")


def synthesize(text: str, who: str = "hearth") -> str | None:
    """Return the path of an mp3 for this line, synthesizing it with Polly on first use. None when unavailable."""
    if not enabled() or not text.strip(): return None
    voice = VOICES.get(who, VOICES["hearth"])
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{who}-{hashlib.sha1((voice + '|' + text).encode()).hexdigest()[:20]}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0: return path
    region = os.environ.get("HEARTH_AWS_REGION", "us-west-2"); profile = os.environ.get("HEARTH_AWS_PROFILE", "alexa-ai-user")
    out = path if not os.environ.get("HEARTH_AWS_CLI", "").startswith("wsl") else "/mnt/" + path[0].lower() + path[2:].replace("\\", "/")
    for engine in (os.environ.get("HEARTH_TTS_ENGINE") or "generative", "neural", "standard"):
        cmd = _cli() + ["polly", "synthesize-speech", "--output-format", "mp3", "--voice-id", voice, "--engine", engine, "--text", text,
                        "--region", region, "--profile", profile, out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0: return path
    return None
