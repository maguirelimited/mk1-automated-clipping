from __future__ import annotations

import argparse
import json

from .service import (
    load_job_payload_from_path,
    make_store,
    publish_due,
    register_and_process_from_payload,
    retry_upload_job,
    schedule_due_upload_jobs,
    schedule_upload_job,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="output-funnel")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db")
    init_db.set_defaults(func=_init_db)

    register = sub.add_parser("register")
    register.add_argument("--report-path", required=True)
    register.set_defaults(func=_register)

    schedule = sub.add_parser("schedule")
    schedule.add_argument("--upload-job-id", type=int)
    schedule.add_argument("--all", action="store_true")
    schedule.add_argument("--limit", type=int)
    schedule.set_defaults(func=_schedule)

    publish = sub.add_parser("publish-due")
    publish.add_argument("--limit", type=int, default=10)
    publish.set_defaults(func=_publish_due)

    retry = sub.add_parser("retry")
    retry.add_argument("--upload-job-id", type=int, required=True)
    retry.set_defaults(func=_retry)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True))


def _init_db(_args: argparse.Namespace) -> dict:
    store = make_store()
    return {"success": True, "database_path": store.db_path}


def _register(args: argparse.Namespace) -> dict:
    payload = load_job_payload_from_path(args.report_path)
    return {"success": True, **register_and_process_from_payload(payload)}


def _schedule(args: argparse.Namespace) -> dict:
    store = make_store()
    if args.all:
        return {"success": True, **schedule_due_upload_jobs(store=store, limit=args.limit)}
    if args.upload_job_id is None:
        raise SystemExit("--upload-job-id is required unless --all is used")
    return {"success": True, **schedule_upload_job(args.upload_job_id, store=store)}


def _publish_due(args: argparse.Namespace) -> dict:
    return {"success": True, **publish_due(limit=args.limit)}


def _retry(args: argparse.Namespace) -> dict:
    return {"success": True, **retry_upload_job(args.upload_job_id)}


if __name__ == "__main__":
    main()
