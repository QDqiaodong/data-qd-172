import argparse
import json
import sys
from pathlib import Path

from . import archive
from .core import cluster


def main():
    argv = sys.argv[1:]
    subcommands = {"archive-init", "publish", "pending", "ack"}
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
    except (OSError, ValueError, KeyError, TypeError, archive.ArchiveError) as exc:
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
