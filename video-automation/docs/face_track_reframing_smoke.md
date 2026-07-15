# Face-track reframing smoke / visual QA

Prompt 8 adds a local smoke harness for comparing `blur_background`, `face_track`, and `auto` reframing on real talking-head clips.

Production default remains `blur_background` until manual review on real footage is satisfactory.

## Optional dependencies

Face detection requires MediaPipe (Python 3.9–3.12) and the bundled BlazeFace model in `video-automation/models/`.

```bash
cd video-automation
source .venv/bin/activate
pip install -r requirements-reframing-optional.txt
```

MediaPipe 0.10+ uses the Tasks API (`FaceDetector`), not the legacy `mp.solutions` API.

Without MediaPipe:

- `blur_background` still runs
- `face_track` fails clearly (strict mode)
- `auto` falls back to blur with warnings

FFmpeg and ffprobe must be on `PATH`.

## Run the smoke harness

Use a local clip you own (podcast, interview, talking-head). Do not commit large copyrighted files.

```bash
cd video-automation
source .venv/bin/activate

python scripts/smoke/smoke_face_track_reframing.py \
  --input /path/to/your_clip.mp4 \
  --output-dir /tmp/face_track_smoke_out
```

Run specific modes:

```bash
python scripts/smoke/smoke_face_track_reframing.py \
  --input /path/to/your_clip.mp4 \
  --output-dir /tmp/face_track_smoke_out \
  --mode blur_background \
  --mode face_track \
  --mode auto
```

## Output files

Typical `output-dir` contents:

| File | Purpose |
|------|---------|
| `input_info.json` | Probed input width/height/duration/audio |
| `blur_background.mp4` | Blur fallback render |
| `face_track.mp4` | Strict face-track crop render (if pipeline succeeds) |
| `auto.mp4` | Auto mode (face-track or blur fallback) |
| `face_track_detection_report.json` | Detection sidecar |
| `face_track_track_report.json` | Tracking sidecar |
| `face_track_crop_path_report.json` | Raw crop path |
| `face_track_smoothed_crop_path_report.json` | Smoothed crop path |
| `face_track_render_report.json` | Render/pipeline readiness metadata |
| `smoke_report.json` | Machine-readable checks + visual QA checklist |

## What to compare visually

1. Play `blur_background.mp4` and `face_track.mp4` side by side.
2. Confirm `face_track.mp4` is full-screen 9:16 crop with no blurred background.
3. Check the speaker stays framed near the upper third and the crop follows horizontal movement without obvious jitter or lag.
4. Review the printed visual QA checklist at the end of the smoke run.
5. Read `smoke_report.json` for `format_strategy`, warnings, and strict-failure reasons.

## Recommended test clip

- 16:9 landscape talking-head footage
- 10–60 seconds
- Single visible speaker facing camera
- Clear face visibility for most of the clip
- Audio present (to verify sync preservation)

## Why default stays `blur_background`

The conveyor default is unchanged until real-video smoke review confirms:

- face detection/tracking is reliable on your content
- crop framing looks better than blur
- `auto` with `face_track_test_enabled=true` and eligibility gating is acceptable in controlled test runs

Use this smoke harness to gather that evidence before changing Ops defaults.

## Face-track test mode (Prompt 15C)

Production default remains `blur_background`. Face-track in `auto` mode requires explicit opt-in:

```text
face_track_test_enabled=false  → auto uses blur (production-safe default)
face_track_test_enabled=true   → auto may use face-track when eligibility passes
```

See [face_track_test_mode.md](face_track_test_mode.md) for the behaviour matrix,
**dev rollout operating procedure** (enable, checklist, Ops UI review, go/no-go),
and smoke examples.
