from __future__ import annotations

import argparse
import json

from .service import (
    backfill_legacy_rows,
    load_job_payload_from_path,
    make_store,
    plan_due_upload_jobs,
    plan_upload_job,
    publish_due,
    register_and_process_from_payload,
    retry_upload_job,
    upload_due,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="output-funnel")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db")
    init_db.set_defaults(func=_init_db)

    register = sub.add_parser("register")
    register.add_argument("--report-path", required=True)
    register.set_defaults(func=_register)

    schedule = sub.add_parser("plan", help="Plan publish_at and upload_at for one or all routed jobs")
    schedule.add_argument("--upload-job-id", type=int)
    schedule.add_argument("--all", action="store_true")
    schedule.add_argument("--limit", type=int)
    schedule.set_defaults(func=_plan)

    schedule_legacy = sub.add_parser(
        "schedule", help="Deprecated: alias for `plan`"
    )
    schedule_legacy.add_argument("--upload-job-id", type=int)
    schedule_legacy.add_argument("--all", action="store_true")
    schedule_legacy.add_argument("--limit", type=int)
    schedule_legacy.set_defaults(func=_plan)

    upload = sub.add_parser("upload-due", help="Upload jobs whose upload window has opened")
    upload.add_argument("--limit", type=int, default=10)
    upload.set_defaults(func=_upload_due)

    publish = sub.add_parser("publish-due", help="Deprecated: alias for `upload-due`")
    publish.add_argument("--limit", type=int, default=10)
    publish.set_defaults(func=_publish_due)

    retry = sub.add_parser("retry")
    retry.add_argument("--upload-job-id", type=int, required=True)
    retry.set_defaults(func=_retry)

    backfill = sub.add_parser(
        "backfill-legacy",
        help="One-off (idempotent) migration of pre-v2 rows to the planned/upload_at model",
    )
    backfill.set_defaults(func=_backfill)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _init_db(_args: argparse.Namespace) -> dict:
    store = make_store()
    return {"success": True, "database_path": store.db_path}


def _register(args: argparse.Namespace) -> dict:
    payload = load_job_payload_from_path(args.report_path)
    return {"success": True, **register_and_process_from_payload(payload)}


def _plan(args: argparse.Namespace) -> dict:
    store = make_store()
    if args.all:
        return {"success": True, **plan_due_upload_jobs(store=store, limit=args.limit)}
    if args.upload_job_id is None:
        raise SystemExit("--upload-job-id is required unless --all is used")
    return {"success": True, **plan_upload_job(args.upload_job_id, store=store)}


def _upload_due(args: argparse.Namespace) -> dict:
    return {"success": True, **upload_due(limit=args.limit)}


def _publish_due(args: argparse.Namespace) -> dict:
    return {"success": True, **publish_due(limit=args.limit)}


def _retry(args: argparse.Namespace) -> dict:
    return {"success": True, **retry_upload_job(args.upload_job_id)}


def _backfill(_args: argparse.Namespace) -> dict:
    return {"success": True, **backfill_legacy_rows()}


if __name__ == "__main__":
    main()
