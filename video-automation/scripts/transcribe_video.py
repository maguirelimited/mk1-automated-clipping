import os
import subprocess
import sys

from pipeline_utils import make_script_error, make_script_success
from mk04_utils import ensure_paths, load_config, resolve_whisper_model_for_transcription
from pipeline_debug_ndjson import write_debug_agent


SCRIPT_NAME = "transcribe_video"
WHISPER_TIMEOUT_SEC = 900


def _derive_transcript_path(input_video: str, output_dir: str) -> str:
    stem = os.path.splitext(os.path.basename(input_video))[0]
    return os.path.join(output_dir, f"{stem}.json")


def run_transcription(input_video: str) -> str:
    video_path = os.path.abspath(input_video)
    output_dir = ensure_paths(load_config())["temp"]

    if not os.path.exists(video_path):
        raise ValueError(f"Input video not found: {video_path}")

    # #region agent log
    write_debug_agent(
        "transcribe-video",
        "H9-transcribe-entry",
        "transcribe_video.py:run_transcription",
        "entered transcription runner",
        {"video_path": video_path, "output_dir": output_dir},
    )
    # #endregion

    print("RUNNING WHISPER...", file=sys.stderr)
    whisper_model = resolve_whisper_model_for_transcription(load_config())
    # #region agent log
    write_debug_agent(
        "transcribe-video",
        "H10-whisper-cli-launch",
        "transcribe_video.py:run_transcription",
        "about to invoke whisper CLI",
        {"timeout_sec": WHISPER_TIMEOUT_SEC, "model": whisper_model},
    )
    # #endregion
    result = subprocess.run(
        [
            "whisper",
            video_path,
            "--model",
            whisper_model,
            "--output_format",
            "json",
            "--output_dir",
            output_dir,
        ],
        timeout=WHISPER_TIMEOUT_SEC,
    )
    # #region agent log
    write_debug_agent(
        "transcribe-video",
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
        write_debug_agent(
            "transcribe-video",
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
