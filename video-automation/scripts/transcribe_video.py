import os
import subprocess
import sys
import json
import time

from pipeline_utils import make_script_error, make_script_success
from mk04_utils import ensure_paths, load_config


SCRIPT_NAME = "transcribe_video"
WHISPER_TIMEOUT_SEC = 900
_CONFIG = load_config()
_DEFAULT_WHISPER_MODEL = (
    _CONFIG.get("models", {}).get("whisper_model", "tiny") or "tiny"
)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", str(_DEFAULT_WHISPER_MODEL)).strip() or str(
    _DEFAULT_WHISPER_MODEL
)
AGENT_DEBUG_LOG_PATH = os.environ.get("AGENT_DEBUG_LOG_PATH", "").strip()
AGENT_DEBUG_SESSION_ID = "35c21b"


def _agent_debug_log(hypothesis_id: str, location: str, message: str, data: dict):
    if not AGENT_DEBUG_LOG_PATH:
        return
    payload = {
        "sessionId": AGENT_DEBUG_SESSION_ID,
        "runId": "general-debug",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(AGENT_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _derive_transcript_path(input_video: str, output_dir: str) -> str:
    stem = os.path.splitext(os.path.basename(input_video))[0]
    return os.path.join(output_dir, f"{stem}.json")


def run_transcription(input_video: str) -> str:
    video_path = os.path.abspath(input_video)
    output_dir = ensure_paths(load_config())["temp"]

    if not os.path.exists(video_path):
        raise ValueError(f"Input video not found: {video_path}")

    # #region agent log
    _agent_debug_log(
        "H9-transcribe-entry",
        "transcribe_video.py:run_transcription",
        "entered transcription runner",
        {"video_path": video_path, "output_dir": output_dir},
    )
    # #endregion

    print("RUNNING WHISPER...", file=sys.stderr)
    # #region agent log
    _agent_debug_log(
        "H10-whisper-cli-launch",
        "transcribe_video.py:run_transcription",
        "about to invoke whisper CLI",
        {"timeout_sec": WHISPER_TIMEOUT_SEC, "model": WHISPER_MODEL},
    )
    # #endregion
    result = subprocess.run(
        [
            "whisper",
            video_path,
            "--model",
            WHISPER_MODEL,
            "--output_format",
            "json",
            "--output_dir",
            output_dir,
        ],
        timeout=WHISPER_TIMEOUT_SEC,
    )
    # #region agent log
    _agent_debug_log(
        "H11-whisper-cli-result",
        "transcribe_video.py:run_transcription",
        "whisper CLI finished",
        {
            "returncode": result.returncode,
        },
    )
    # #endregion

    if result.returncode != 0:
        raise RuntimeError("Whisper failed")
    transcript_path = _derive_transcript_path(video_path, output_dir)
    if not os.path.isfile(transcript_path):
        raise RuntimeError(f"Transcript file missing: {transcript_path}")
    return transcript_path


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("DEBUG"):
        print("ARGV:", sys.argv if argv is None else argv, file=sys.stderr)

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 1:
            raise ValueError("No input video provided")
        transcript_path = run_transcription(args[0])
        print(f"[SUCCESS] Transcription complete for {args[0]}", file=sys.stderr)
        print(make_script_success(SCRIPT_NAME, transcript_path=transcript_path))
        return 0
    except Exception as e:
        # #region agent log
        _agent_debug_log(
            "H12-transcribe-exception",
            "transcribe_video.py:main",
            "transcription script exception",
            {"error": str(e)},
        )
        # #endregion
        print(f"[ERROR] Transcription failed: {e}", file=sys.stderr)
        print(make_script_error(SCRIPT_NAME, str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
