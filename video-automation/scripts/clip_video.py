import os
import json
import subprocess
import sys
import uuid
import time
from datetime import timedelta

from pipeline_utils import make_script_error, make_script_success, validate_segment_times
from mk04_utils import ensure_paths, load_config


SCRIPT_NAME = "clip_video"
FFMPEG_TIMEOUT_SEC = 120
DEBUG_MODE_LOG_PATH = os.environ.get("DEBUG_MODE_LOG_PATH", "").strip()
DEBUG_MODE_SESSION_ID = "c9492c"


def _debug_mode_log(hypothesis_id: str, location: str, message: str, data: dict):
    if not DEBUG_MODE_LOG_PATH:
        return
    payload = {
        "sessionId": DEBUG_MODE_SESSION_ID,
        "runId": "clip-video",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(DEBUG_MODE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _ffprobe_duration_sec(path: str) -> float | None:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return None
        return float((p.stdout or "").strip())
    except Exception:
        return None


def _ffmpeg_duration_arg(start: str, end: str) -> str:
    def _parse(ts: str) -> float:
        parts = str(ts).strip().split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid timestamp format: {ts}")
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s

    duration_sec = _parse(end) - _parse(start)
    if duration_sec <= 0:
        raise ValueError("Clip duration must be > 0 seconds")
    # ffmpeg accepts HH:MM:SS.mmm for -t
    td = timedelta(seconds=duration_sec)
    total = td.total_seconds()
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def run_clip(video_path: str, start: str, end: str, output_path: str | None = None) -> str:
    input_video = os.path.abspath(video_path)
    start = str(start).strip()
    end = str(end).strip()

    output_dir = ensure_paths(load_config())["output"]

    if output_path:
        output_video = os.path.abspath(output_path)
    else:
        stem = os.path.splitext(os.path.basename(input_video))[0]
        output_video = os.path.join(output_dir, f"{stem}_clip_{uuid.uuid4().hex[:10]}.mp4")

    if not os.path.exists(input_video):
        raise ValueError(f"Input video not found: {input_video}")

    start_sec, end_sec = validate_segment_times(start, end)
    duration = _ffmpeg_duration_arg(start, end)
    expected_duration_sec = end_sec - start_sec
    input_duration_sec = _ffprobe_duration_sec(input_video)
    if input_duration_sec is not None and (
        start_sec >= input_duration_sec or end_sec > input_duration_sec
    ):
        raise ValueError(
            "Requested clip timestamps exceed input video duration "
            f"({input_duration_sec:.3f}s)"
        )

    # Stream copy is fast but cuts only on keyframes; timestamps can drift slightly vs exact HH:MM:SS.
    print("RUNNING FFMPEG...", file=sys.stderr)
    print(f"START: {start} | END: {end}", file=sys.stderr)

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            start,
            "-i",
            input_video,
            "-t",
            duration,
            "-c",
            "copy",
            output_video,
        ],
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    if result.returncode != 0:
        raise RuntimeError("FFmpeg failed")
    if not os.path.isfile(output_video) or os.path.getsize(output_video) == 0:
        raise RuntimeError("FFmpeg did not produce a valid output file")
    output_duration_sec = _ffprobe_duration_sec(output_video)
    if output_duration_sec is None:
        raise RuntimeError("Could not read clip duration via ffprobe")
    if output_duration_sec < min(0.5, expected_duration_sec * 0.1):
        raise RuntimeError(
            f"Clip output duration too small ({output_duration_sec:.3f}s vs expected {expected_duration_sec:.3f}s)"
        )
    # #region agent log
    _debug_mode_log(
        "H5-tiny-output-passes",
        "clip_video.py:run_clip",
        "clip output quality metrics",
        {
            "output_video": output_video,
            "output_size_bytes": os.path.getsize(output_video),
            "expected_duration_sec": round(expected_duration_sec, 3),
            "ffprobe_output_sec": output_duration_sec,
            "input_duration_sec": input_duration_sec,
        },
    )
    # #endregion
    return output_video


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 3:
            raise ValueError("Missing arguments: need video_path start end [output_path]")

        output_video = run_clip(
            args[0],
            args[1],
            args[2],
            args[3] if len(args) >= 4 else None,
        )
        # stdout line 1 remains path for compatibility with older callers.
        print(output_video)
        print(make_script_success(SCRIPT_NAME, output_path=output_video))
        return 0
    except Exception as e:
        print(f"[ERROR] Clipping failed: {e}", file=sys.stderr)
        print(make_script_error(SCRIPT_NAME, str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
