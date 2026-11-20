import argparse
import json
import sys
from pathlib import Path

from . import archive
from . import coverage
from . import review
from . import reconcile
from .core import cluster


def main():
    argv = sys.argv[1:]
    subcommands = {"archive-init", "publish", "pending", "ack",
                   "review-init", "review-import", "review-current",
                   "review-todo", "review-assign", "review-conclude",
                   "review-history", "reconcile", "coverage-audit"}
    if argv and argv[0] in subcommands:
        _run_subcommand(argv)
    elif argv and argv[0] in ("-h", "--help"):
        _build_parser().print_help()
    else:
        # Legacy invocation: fire-cluster --input ... --output ...
        _run_cluster(argv)


def _build_parser():
    p = argparse.ArgumentParser(
        prog="fire-cluster",
        epilog="legacy report form: fire-cluster --input OBS.json --output REPORT.json")
    sub = p.add_subparsers(dest="command")

    init = sub.add_parser("archive-init", help="create a local publishing archive")
    init.add_argument("--archive", required=True)
    init.add_argument("--consumer", action="append", required=True,
                      help="consumer id to register (repeatable, at least one)")

    publish = sub.add_parser("publish", help="publish one observation batch")
    publish.add_argument("--archive", required=True)
    publish.add_argument("--input", required=True)
    publish.add_argument("--batch-id", required=True)

    pending = sub.add_parser("pending", help="list a consumer's unacknowledged records")
    pending.add_argument("--archive", required=True)
    pending.add_argument("--consumer", required=True)

    ack = sub.add_parser("ack", help="acknowledge one record for one consumer")
    ack.add_argument("--archive", required=True)
    ack.add_argument("--consumer", required=True)
    ack.add_argument("--record", required=True)

    rinit = sub.add_parser("review-init", help="create a local review workbench")
    rinit.add_argument("--workbench", required=True)

    rimport = sub.add_parser("review-import",
                             help="import the full current observation set")
    rimport.add_argument("--workbench", required=True)
    rimport.add_argument("--input", required=True)

    rcurrent = sub.add_parser("review-current",
                              help="list active review cases")
    rcurrent.add_argument("--workbench", required=True)

    rtodo = sub.add_parser("review-todo",
                           help="list active cases still needing review")
    rtodo.add_argument("--workbench", required=True)
    todo_filter = rtodo.add_mutually_exclusive_group()
    todo_filter.add_argument("--owner", help="only cases owned by this id")
    todo_filter.add_argument("--unassigned", action="store_true",
                             help="only cases without an owner")

    rassign = sub.add_parser("review-assign",
                             help="assign the owner of a pending case")
    rassign.add_argument("--workbench", required=True)
    rassign.add_argument("--case", required=True)
    rassign.add_argument("--expected-revision", required=True, type=int)
    rassign.add_argument("--actor", required=True)
    rassign.add_argument("--owner", required=True)
    rassign.add_argument("--reason", required=True)

    rconclude = sub.add_parser("review-conclude",
                               help="record a human conclusion for a case")
    rconclude.add_argument("--workbench", required=True)
    rconclude.add_argument("--case", required=True)
    rconclude.add_argument("--expected-revision", required=True, type=int)
    rconclude.add_argument("--actor", required=True)
    rconclude.add_argument("--verdict", required=True,
                           choices=sorted(review.VERDICTS))
    rconclude.add_argument("--note", required=True)

    rhistory = sub.add_parser("review-history",
                              help="show the audit trail of one case")
    rhistory.add_argument("--workbench", required=True)
    rhistory.add_argument("--case", required=True)

    rec = sub.add_parser("reconcile",
                         help="reconcile primary/backup gateway exports into "
                              "a replayable clustering input")
    rec.add_argument("--input", required=True)
    rec.add_argument("--output", required=True)

    audit = sub.add_parser("coverage-audit",
                           help="audit planned sampling coverage and attach "
                                "an evidence-completeness verdict to each "
                                "candidate incident")
    audit.add_argument("--input", required=True,
                       help="full observation JSON (same shape as --input)")
    audit.add_argument("--schedule", required=True,
                       help="sampling plan JSON with per-sensor active "
                            "window, cadence and phase")
    audit.add_argument("--start", required=True,
                       help="audit window start, inclusive (UTC offset)")
    audit.add_argument("--end", required=True,
                       help="audit window end, exclusive (UTC offset)")
    audit.add_argument("--tolerance-seconds", required=True, type=int,
                       help="must be strictly below half of every planned "
                            "cadence_seconds")
    audit.add_argument("--minimum-coverage", required=True, type=float,
                       help="incident threshold within [0, 1]")
    audit.add_argument("--output", required=True)
    return p


def _run_subcommand(argv):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "archive-init":
            archive.init_archive(args.archive, args.consumer)
            print(json.dumps({"archive": args.archive,
                              "consumers": list(args.consumer)}, sort_keys=True))
        elif args.command == "publish":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            receipt = archive.publish(args.archive, args.batch_id, payload)
            print(json.dumps(receipt, sort_keys=True))
        elif args.command == "pending":
            records = archive.pending(args.archive, args.consumer)
            print(json.dumps(records, sort_keys=True))
        elif args.command == "ack":
            result = archive.acknowledge(args.archive, args.consumer, args.record)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "review-init":
            result = review.init_workbench(args.workbench)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "review-import":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            receipt = review.import_observations(args.workbench, payload)
            print(json.dumps(receipt, sort_keys=True))
        elif args.command == "review-current":
            print(json.dumps(review.current_cases(args.workbench),
                             sort_keys=True))
        elif args.command == "review-todo":
            cases = review.todo_cases(args.workbench, owner=args.owner,
                                      unassigned=args.unassigned)
            print(json.dumps(cases, sort_keys=True))
        elif args.command == "review-assign":
            result = review.assign_owner(args.workbench, args.case,
                                         args.expected_revision, args.actor,
                                         args.owner, args.reason)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "review-conclude":
            result = review.submit_conclusion(args.workbench, args.case,
                                              args.expected_revision,
                                              args.actor, args.verdict,
                                              args.note)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "review-history":
            result = review.case_history(args.workbench, args.case)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "reconcile":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            value = reconcile.reconcile(payload)
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x") as f:
                json.dump(value, f, indent=2)
                f.write("\n")
            summary = value["reconciliation"]
            print(json.dumps({
                "observations": len(value["observations"]),
                "max_duplicate_pairs": summary["max_duplicate_pairs"],
                "included_backup_ids": [
                    row["id"] for row in summary["backup_rows"]
                    if row["decision"] == "included"],
            }, sort_keys=True))
        elif args.command == "coverage-audit":
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
            schedule = json.loads(
                Path(args.schedule).read_text(encoding="utf-8"))
            value = coverage.audit_coverage(
                data, schedule, start=args.start, end=args.end,
                tolerance_seconds=args.tolerance_seconds,
                minimum_coverage=args.minimum_coverage)
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x") as f:
                json.dump(value, f, indent=2)
                f.write("\n")
            print(json.dumps({
                "observations": value["cluster_report"]["observation_count"],
                "incidents": len(value["incidents"]),
                "coverage": value["coverage"]["coverage_ratio"],
                "eligible_slots": value["coverage"]["eligible_slots"],
                "missing_slots": value["coverage"]["missing_slots"],
            }, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, archive.ArchiveError,
            review.ReviewError) as exc:
        _build_parser().exit(2, f"fire-cluster: {exc}\n")


def _run_cluster(argv):
    p = argparse.ArgumentParser(prog="fire-cluster")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args(argv)
    try:
        value = cluster(json.loads(Path(a.input).read_text()))
        target = Path(a.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        print(json.dumps({"observations": value["observation_count"], "incidents": len(value["incidents"])}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"fire-cluster: {exc}\n")


if __name__ == "__main__":
    main()
