import math
import os
import subprocess
import sys
import uuid
from datetime import timedelta

from pipeline_utils import make_script_error, make_script_success, validate_segment_times
from mk04_utils import ensure_paths, ffprobe_demux_json, ffprobe_duration_sec, load_config
from pipeline_debug_ndjson import write_debug_mode


SCRIPT_NAME = "clip_video"
FFMPEG_TIMEOUT_SEC = 120
CLIP_DECODE_PROBE_TIMEOUT_SEC = 90

# Tests patch this symbol; implementation lives in ``mk04_utils.ffprobe_demux_json``.
_ffprobe_demux_json = ffprobe_demux_json


def clip_duration_tolerance_sec(expected_duration_sec: float) -> tuple[float, float]:
    """Inclusive-ish bounds [min, max] for ffprobe-measured clip duration vs request.

    Stream copy `-c copy` + seek can land short of nominal wall-clock; bounds stay
    strict enough to reject obviously hollow/truncated outputs.
    """
    exp = float(expected_duration_sec)
    if not math.isfinite(exp) or exp <= 0:
        return (0.0, 0.0)
    min_d = max(0.55, exp * 0.62)
    max_d = exp * 1.10 + 0.75
    return (min_d, max_d)


def _first_video_stream(demux: dict) -> dict | None:
    for s in demux.get("streams") or []:
        if isinstance(s, dict) and str(s.get("codec_type")) == "video":
            return s
    return None


def _effective_probe_duration_sec(demux: dict, video_stream: dict) -> float | None:
    fmt = demux.get("format")
    candidates: list[float] = []
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            d = float(fmt["duration"])
            if math.isfinite(d) and d > 0:
                candidates.append(d)
        except (TypeError, ValueError):
            pass
    if video_stream.get("duration") is not None:
        try:
            d = float(video_stream["duration"])
            if math.isfinite(d) and d > 0:
                candidates.append(d)
        except (TypeError, ValueError):
            pass
    if not candidates:
        return None
    return max(candidates)


def _ffmpeg_null_decode(path: str, *, timeout_sec: int = CLIP_DECODE_PROBE_TIMEOUT_SEC) -> None:
    r = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            path,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if r.returncode != 0:
        err = (r.stderr or "").strip()[:800]
        raise RuntimeError(
            f"CLIP_REJECTED decode_sanity_failed: full demux/decode pass failed ({err or 'no stderr'})"
        )


def _assert_clip_output_matches_request(output_path: str, expected_duration_sec: float) -> dict:
    """Fail loudly unless the mux looks like real video and durations are plausible."""

    abs_out = os.path.abspath(output_path)
    if not os.path.isfile(abs_out):
        raise RuntimeError("CLIP_REJECTED missing_output_file")
    size = os.path.getsize(abs_out)
    if size < 256:
        raise RuntimeError(
            f"CLIP_REJECTED output_too_small_bytes: {size}b (possible empty/corrupted mux)"
        )

    expected = float(expected_duration_sec)
    if not math.isfinite(expected) or expected <= 0:
        raise RuntimeError("CLIP_REJECTED internal_expected_duration_invalid")

    min_dur, max_dur = clip_duration_tolerance_sec(expected)

    demux = _ffprobe_demux_json(abs_out)
    video = _first_video_stream(demux)
    if not video:
        raise RuntimeError(
            "CLIP_REJECTED no_video_stream: container has no decodable video track"
        )
    codec = str(video.get("codec_name") or "").strip().lower()
    if not codec or codec == "unknown":
        raise RuntimeError("CLIP_REJECTED video_codec_unusable")

    try:
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
    except (TypeError, ValueError):
        w, h = 0, 0
    if w < 16 or h < 16:
        raise RuntimeError(f"CLIP_REJECTED video_dimensions_invalid: {w}x{h}")

    dur = _effective_probe_duration_sec(demux, video)
    if dur is None or not math.isfinite(dur) or dur <= 0:
        raise RuntimeError(
            "CLIP_REJECTED unreadable_output_duration: ffprobe reported no usable duration "
            "for mux or video stream"
        )

    if dur + 1e-3 < min_dur:
        raise RuntimeError(
            f"CLIP_REJECTED output_duration_too_short: measured {dur:.3f}s vs requested≈"
            f"{expected:.3f}s (reject below {min_dur:.3f}s — likely truncation/corruption)"
        )
    if dur > max_dur:
        raise RuntimeError(
            f"CLIP_REJECTED output_duration_too_long: measured {dur:.3f}s vs requested≈"
            f"{expected:.3f}s (reject above {max_dur:.3f}s)"
        )

    bytes_per_second = float(size) / dur
    floor_bps = 400.0
    if bytes_per_second < floor_bps:
        raise RuntimeError(
            f"CLIP_REJECTED implausible_output_size: {size}b over {dur:.3f}s "
            f"(≈{bytes_per_second:.0f} B/s below floor {floor_bps:.0f} B/s)"
        )

    _ffmpeg_null_decode(abs_out)

    fmt_out = {}
    fmt = demux.get("format")
    if isinstance(fmt, dict):
        fmt_out["format_long_name"] = str(fmt.get("format_long_name") or "")
        fmt_out["bit_rate_reported"] = fmt.get("bit_rate")

    validation: dict[str, object] = {
        "ok": True,
        "ffprobe_duration_sec": round(dur, 3),
        "expected_duration_sec": round(expected, 3),
        "duration_min_accept_sec": round(min_dur, 3),
        "duration_max_accept_sec": round(max_dur, 3),
        "width": w,
        "height": h,
        "codec_name": str(video.get("codec_name") or ""),
        "size_bytes": size,
        "bytes_per_second_rounded": round(bytes_per_second, 1),
        "format": fmt_out,
    }

    write_debug_mode(
        "clip-video",
        "H5-clip-output-validated",
        "clip_video.py:_assert_clip_output_matches_request",
        "clip passes demux/size/duration/decode checks",
        {k: validation[k] for k in validation if isinstance(validation[k], (int, float, str))},
    )
    return validation


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


def run_clip(video_path: str, start: str, end: str, output_path: str | None = None) -> tuple[str, dict]:
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
    input_duration_sec = ffprobe_duration_sec(input_video)
    if input_duration_sec is None or input_duration_sec <= 0:
        raise ValueError(
            "CLIP_REJECTED unavailable_input_duration: ffprobe could not read a positive "
            "duration for the input file — refusing to slice without grounded media bounds."
        )
    if start_sec >= input_duration_sec or end_sec > input_duration_sec:
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
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    if result.returncode != 0:
        err = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[:1400]
        raise RuntimeError(
            f"FFmpeg failed (exit {result.returncode}): {err or '(no output)'}"
        )
    if not os.path.isfile(output_video) or os.path.getsize(output_video) == 0:
        raise RuntimeError("FFmpeg did not produce a non-empty output file")

    validation = _assert_clip_output_matches_request(output_video, expected_duration_sec)

    return output_video, validation


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 3:
            raise ValueError("Missing arguments: need video_path start end [output_path]")

        output_video, validation = run_clip(
            args[0],
            args[1],
            args[2],
            args[3] if len(args) >= 4 else None,
        )
        # stdout line 1 remains path for compatibility with older callers.
        print(output_video)
        print(
            make_script_success(
                SCRIPT_NAME,
                output_path=output_video,
                clip_validation=validation,
            )
        )
        return 0
    except Exception as e:
        print(f"[ERROR] Clipping failed: {e}", file=sys.stderr)
        print(make_script_error(SCRIPT_NAME, str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
