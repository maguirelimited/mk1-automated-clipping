#!/usr/bin/env bash
# Quick manual checks for content funnel visibility (no extra services).
# Run from repo root or video-automation; requires a running clipper and a file in input/.
#
# 1) Default funnel (uses defaults.default_funnel_id in pipeline_config.json when set):
#    curl -sS -X POST http://127.0.0.1:5050/jobs \
#      -H 'Content-Type: application/json' \
#      -d '{"video":"YOUR_FILE.mp4"}' | jq '{funnel_id, funnel_name, enabled_platforms, funnel_policy_summary, funnel}'
#
# 2) Explicit funnel:
#    curl -sS -X POST http://127.0.0.1:5050/jobs \
#      -H 'Content-Type: application/json' \
#      -d '{"video":"YOUR_FILE.mp4","funnel_id":"business_clips_test"}' | jq '{funnel_id, enabled_platforms, funnel}'
#
# 3) After a run, open job report JSON (path in response report_path) and confirm keys:
#    funnel.funnel_id, funnel.funnel_name, funnel.enabled_platforms,
#    funnel.resolved_selection, funnel.resolved_output, funnel.funnel_policy_summary
#
# 4) Open review.md in the same job directory — it should list "## Funnel" with the same ids/settings.
#
# 5) Prove config-only changes: edit config/funnels/business_clips_test.json (e.g. max_clips, filename_prefix),
#    rerun the same curl without code changes; compare report.json + output filenames (prefix_clip_01_*.mp4).

set -euo pipefail
echo "See comments in $0 for manual verification steps."
