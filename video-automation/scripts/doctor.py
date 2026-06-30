import json
import os
import shutil
import subprocess
import sys

from mk04_utils import ensure_paths, load_config, resolve_transcribe_engine


def main() -> int:
    config = load_config()
    paths = ensure_paths(config)
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "missing")

    active_engine = resolve_transcribe_engine()
    whisper_path = shutil.which("whisper")
    if active_engine == "whisperx":
        try:
            import whisperx  # noqa: F401

            add("transcribe_engine:whisperx", True, "import ok (active engine)")
        except Exception as exc:
            add("transcribe_engine:whisperx", False, repr(exc))
        add(
            "whisper_cli (legacy fallback)",
            True,
            whisper_path or "not on PATH — not required while TRANSCRIBE_ENGINE=whisperx",
        )
    else:
        add("whisper", bool(whisper_path), whisper_path or "missing")
    add(
        "OPENAI_API_KEY",
        bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "set" if os.environ.get("OPENAI_API_KEY", "").strip() else "missing",
    )
    for key, path in paths.items():
        add(f"path:{key}", os.path.isdir(path), path)

    try:
        p = subprocess.run(
            ["python3", "-c", "import flask; print('ok')"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        add("flask_import", p.returncode == 0, (p.stdout or p.stderr).strip())
    except Exception as e:
        add("flask_import", False, str(e))

    ok = all(bool(c["ok"]) for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
