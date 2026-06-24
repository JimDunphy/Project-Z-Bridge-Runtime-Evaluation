#!/usr/bin/env python3
"""Render and redact Project Z-Bridge compatibility trace JSONL for humans."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


@dataclass
class MethodStats:
    calls: int = 0
    faults: int = 0
    private: int = 0
    durations: list[int] = field(default_factory=list)
    statuses: Counter[str] = field(default_factory=Counter)
    fault_codes: Counter[str] = field(default_factory=Counter)
    shape_examples: Counter[str] = field(default_factory=Counter)


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bool_value(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def contains_private(record: dict[str, Any]) -> bool:
    return (
        bool_value(record.get("containsPrivate"))
        or bool_value(record.get("containsValues"))
        or bool_value(record.get("containsFull"))
        or isinstance(record.get("private"), dict)
        or isinstance(record.get("responseValues"), dict)
        or "fullRequest" in record
        or "fullResponse" in record
    )


def private_value_to_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def flatten_private(value: Any, prefix: str = "", limit: int = 16) -> list[str]:
    parts: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            parts.extend(flatten_private(value[key], child_prefix, limit=limit))
            if len(parts) >= limit:
                return parts[:limit]
        return parts
    if isinstance(value, list):
        for idx, item in enumerate(value[:4]):
            child_prefix = f"{prefix}[{idx}]"
            parts.extend(flatten_private(item, child_prefix, limit=limit))
            if len(parts) >= limit:
                return parts[:limit]
        if len(value) > 4:
            parts.append(f"{prefix}.more={len(value) - 4}")
        return parts[:limit]
    if prefix:
        parts.append(f"{prefix}={private_value_to_string(value)}")
    return parts


def private_summary(record: dict[str, Any], limit: int = 120) -> str:
    private = record.get("private")
    if not isinstance(private, dict) or not private:
        return ""
    rendered = " ".join(flatten_private(private))
    return one_line(rendered, limit)


def response_values_summary(record: dict[str, Any], limit: int = 120) -> str:
    values = record.get("responseValues")
    if not isinstance(values, dict) or not values:
        return ""
    rendered = " ".join(flatten_private(values))
    return one_line(rendered, limit)


def one_line(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def fmt_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    # Keep the output compact while preserving UTC visibility.
    if "T" in value:
        time_part = value.split("T", 1)[1]
        time_part = time_part.replace("+00:00", "Z")
        if time_part.endswith("Z"):
            time_part = time_part[:-1]
        return time_part.split(".", 1)[0] + "Z"
    return value


def fmt_ms(values: list[int]) -> tuple[str, str]:
    if not values:
        return ("0.0", "0")
    return (f"{mean(values):.1f}", str(max(values)))


def load_records(paths: list[str]) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid = 0

    def read_lines(path: str) -> Iterable[str]:
        if path == "-":
            yield from sys.stdin
            return
        yield from Path(path).read_text(encoding="utf-8").splitlines()

    for path in paths or ["-"]:
        try:
            iterator = read_lines(path)
            for line in iterator:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    invalid += 1
        except FileNotFoundError:
            print(f"compat-trace: file not found: {path}", file=sys.stderr)
            sys.exit(2)
        except OSError as exc:
            print(f"compat-trace: failed to read {path}: {exc}", file=sys.stderr)
            sys.exit(2)

    return records, invalid


def redact_records(paths: list[str]) -> int:
    invalid = 0

    def read_lines(path: str) -> Iterable[str]:
        if path == "-":
            yield from sys.stdin
            return
        yield from Path(path).read_text(encoding="utf-8").splitlines()

    for path in paths or ["-"]:
        try:
            for line in read_lines(path):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if not isinstance(record, dict):
                    invalid += 1
                    continue
                record.pop("private", None)
                record.pop("responseValues", None)
                record.pop("fullRequest", None)
                record.pop("fullResponse", None)
                record["containsPrivate"] = False
                record["containsValues"] = False
                record["containsFull"] = False
                print(json.dumps(record, separators=(",", ":"), sort_keys=True))
        except FileNotFoundError:
            print(f"compat-trace: file not found: {path}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"compat-trace: failed to read {path}: {exc}", file=sys.stderr)
            return 2

    if invalid:
        print(f"compat-trace: skipped {invalid} invalid JSONL line(s)", file=sys.stderr)
    return 0


def read_line_as_record(line: str) -> tuple[dict[str, Any] | None, bool]:
    line = line.strip()
    if not line:
        return None, False
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None, True
    if isinstance(record, dict):
        return record, False
    return None, True


def follow_records(path: str, from_start: bool = False, poll_secs: float = 0.5) -> Iterable[dict[str, Any]]:
    if path == "-":
        for line in sys.stdin:
            record, _invalid = read_line_as_record(line)
            if record is not None:
                yield record
        return

    trace_path = Path(path)
    position = 0
    opened = False

    while True:
        try:
            with trace_path.open("r", encoding="utf-8") as handle:
                if not opened:
                    if from_start:
                        handle.seek(0)
                    else:
                        handle.seek(0, 2)
                    position = handle.tell()
                    opened = True
                else:
                    size = trace_path.stat().st_size
                    if size < position:
                        # File was rotated/truncated. Start at the beginning of the new file.
                        position = 0
                    handle.seek(position)

                while True:
                    line = handle.readline()
                    if not line:
                        position = handle.tell()
                        break
                    record, _invalid = read_line_as_record(line)
                    if record is not None:
                        yield record
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"compat-trace: follow read failed for {path}: {exc}", file=sys.stderr)

        time.sleep(max(0.1, poll_secs))


def shape_parts(record: dict[str, Any], show_private: bool = False) -> list[str]:
    shape = record.get("shape")
    if not isinstance(shape, dict):
        shape = {}
    parts: list[str] = []

    for key in [
        "types",
        "fmt",
        "view",
        "disp",
        "l",
        "sortBy",
        "offset",
        "limit",
        "actionOp",
        "actionL",
        "actionIdCount",
        "idsCount",
        "messageRequest",
        "messageIdPresent",
        "messageIdCount",
        "messageHtml",
        "messageRead",
        "messageMax",
        "messagePart",
        "messageRaw",
        "composeMessage",
        "composeIDPresent",
        "composeORIGIDPresent",
        "composeRTPresent",
        "composeSUPresent",
        "composeAddressCount",
        "composeAttachPresent",
        "composePartCount",
        "contentType",
        "bodyBytes",
    ]:
        value = shape.get(key)
        if value is not None:
            parts.append(f"{key}={display_value(value)}")

    query = shape.get("query")
    if isinstance(query, dict) and bool_value(query.get("present")):
        safe = query.get("safeValue")
        kind = query.get("inTermKind")
        token_count = query.get("tokenCount")
        if safe:
            parts.append(f"query={safe}")
        elif kind:
            parts.append(f"query={kind}")
        else:
            parts.append("query=<redacted>")
        if token_count is not None:
            parts.append(f"tokens={token_count}")

    for key in ["idPresent", "filenamePresent", "partPresent", "callbackPresent", "lbfumsPresent"]:
        value = shape.get(key)
        if value is True:
            parts.append(f"{key}=true")

    batch = record.get("batchMethods")
    if isinstance(batch, list) and batch:
        parts.append("batch=" + ",".join(str(item) for item in batch[:6]))

    request_keys = shape.get("requestKeys")
    if isinstance(request_keys, list) and request_keys:
        visible = [str(item) for item in request_keys[:8]]
        parts.append("keys=" + ",".join(visible))

    if not parts:
        parts.append("<no shape fields>")
    if show_private:
        private = private_summary(record)
        if private:
            parts.append("values=" + private)
    return parts


def shape_summary(record: dict[str, Any], limit: int = 92, show_private: bool = False) -> str:
    return one_line(" ".join(shape_parts(record, show_private=show_private)), limit)


def response_parts(record: dict[str, Any], show_values: bool = False) -> list[str]:
    shape = record.get("responseShape")
    if not isinstance(shape, dict):
        shape = {}
    parts: list[str] = []

    for key in [
        "responseName",
        "conversationCount",
        "messageCount",
        "appointmentCount",
        "contactCount",
        "folderCount",
        "linkCount",
        "documentCount",
        "tagCount",
        "messageIdPresent",
        "messagePartCount",
        "more",
        "offset",
        "sortBy",
    ]:
        value = shape.get(key)
        if value is not None:
            parts.append(f"{key}={display_value(value)}")

    for key, label in [("responseKeys", "respKeys"), ("batchResponses", "batchResp")]:
        values = shape.get(key)
        if isinstance(values, list) and values:
            parts.append(label + "=" + ",".join(str(item) for item in values[:8]))

    if record.get("faultCode"):
        parts.append(f"faultCode={record.get('faultCode')}")
    elif not parts:
        parts.append(f"status={record.get('status') or '?'}")

    if show_values:
        values = response_values_summary(record)
        if values:
            parts.append("values=" + values)
    return parts


def response_summary(record: dict[str, Any], limit: int = 92, show_values: bool = False) -> str:
    return one_line(" ".join(response_parts(record, show_values=show_values)), limit)


def flow_summary(record: dict[str, Any], limit: int = 160, show_values: bool = False) -> str:
    req_limit = max(40, limit // 2)
    resp_limit = max(40, limit - req_limit - 4)
    req = shape_summary(record, req_limit, show_private=show_values)
    resp = response_summary(record, resp_limit, show_values=show_values)
    return one_line(f"REQ {req} -> RESP {resp}", limit)


def aggregate(records: list[dict[str, Any]], show_private: bool = False) -> dict[tuple[str, str, str], MethodStats]:
    stats: dict[tuple[str, str, str], MethodStats] = defaultdict(MethodStats)
    for record in records:
        key = (
            str(record.get("kind") or "?").upper(),
            str(record.get("method") or "?"),
            str(record.get("route") or "?"),
        )
        item = stats[key]
        item.calls += 1
        if bool_value(record.get("fault")):
            item.faults += 1
        if contains_private(record):
            item.private += 1
        item.durations.append(int_value(record.get("durationMs")))
        item.statuses[str(record.get("status") or "?")] += 1
        fault_code = record.get("faultCode")
        if fault_code:
            item.fault_codes[str(fault_code)] += 1
        example_limit = 220 if show_private else 120
        item.shape_examples[flow_summary(record, example_limit, show_values=show_private)] += 1
    return dict(stats)


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    print(line(headers))
    print(line(["-" * width for width in widths]))
    for row in rows:
        print(line(row))


def table_line(headers: list[str], row: list[str], widths: list[int]) -> str:
    values = row[:]
    for idx, width in enumerate(widths):
        values[idx] = one_line(values[idx], width).ljust(width)
    return "  ".join(values)


def is_noop_record(record: dict[str, Any]) -> bool:
    return str(record.get("method") or "") == "NoOpRequest"


def default_curl_base_url() -> str:
    configured = os.environ.get("BRIDGE_COMPAT_TRACE_CURL_BASE", "").strip()
    if configured:
        return configured.rstrip("/")
    port = os.environ.get("BRIDGE_PORT", "7777").strip() or "7777"
    return f"http://127.0.0.1:{port}"


def render_pretty_json(label: str, value: Any) -> None:
    print(label)
    print("-" * len(label))
    if value is None:
        print("<not captured>")
        return
    print(json.dumps(value, indent=2, ensure_ascii=False))


def record_url(record: dict[str, Any], base_url: str) -> str:
    route = str(record.get("route") or "/service/soap")
    if not route.startswith("/"):
        route = "/" + route
    return base_url.rstrip("/") + route


def render_curl_replay(record: dict[str, Any], base_url: str) -> None:
    request = record.get("fullRequest")
    if not isinstance(request, dict):
        print("# No fullRequest block was captured for this record; cannot generate curl replay.")
        return
    if str(record.get("kind") or "").lower() != "soap":
        print("# curl replay currently supports full SOAP JSON records.")
        return
    print("CURL REPLAY")
    print("-----------")
    print(f"curl -sS {json.dumps(record_url(record, base_url))} \\")
    print("  -H 'content-type: application/json' \\")
    print("  -d @- <<'JSON'")
    print(json.dumps(request, indent=2, ensure_ascii=False))
    print("JSON")


def render_full_dump(
    records: list[dict[str, Any]],
    tail: int,
    method: str | None,
    curl: bool,
    base_url: str,
) -> None:
    selected = records
    if method:
        selected = [record for record in selected if str(record.get("method") or "") == method]
    selected = [
        record
        for record in selected
        if "fullRequest" in record or "fullResponse" in record
    ]
    if tail > 0:
        selected = list(deque(selected, maxlen=tail))

    if not selected:
        print("No full compatibility trace records found.")
        print("Enable BRIDGE_COMPAT_TRACE_ENABLED=1 and BRIDGE_COMPAT_TRACE_DETAIL=full, restart the bridge, exercise the client, then retry.")
        return

    for idx, record in enumerate(selected, start=1):
        if idx > 1:
            print("")
            print("=" * 80)
            print("")
        print(
            f"{fmt_time(record.get('ts'))} "
            f"{str(record.get('kind') or '?').upper()} "
            f"{record.get('method') or '?'} "
            f"{record.get('route') or '?'} "
            f"status={record.get('status') or '?'} "
            f"fault={'yes' if bool_value(record.get('fault')) else 'no'} "
            f"ms={record.get('durationMs') or 0}"
        )
        print(flow_summary(record, 260, show_values=True))
        print("")
        if curl:
            render_curl_replay(record, base_url)
        else:
            render_pretty_json("REQUEST", record.get("fullRequest"))
            print("")
            render_pretty_json("RESPONSE", record.get("fullResponse"))


def render_summary(
    records: list[dict[str, Any]],
    invalid: int,
    source: str,
    hidden_noop: int,
    show_private: bool,
) -> None:
    print("Project Z-Bridge Compatibility Trace")
    print("=" * 36)
    print(f"Source: {source}")
    print(f"Records: {len(records)}")
    if hidden_noop:
        print(f"Mode: foreground client actions ({hidden_noop} NoOpRequest heartbeat records hidden; use --all to include them)")
    else:
        print("Mode: all visible records")
    if invalid:
        print(f"Invalid JSONL lines skipped: {invalid}")
    private_count = sum(1 for record in records if contains_private(record))
    if private_count:
        print(f"Value detail blocks: {private_count} record(s)")
        if show_private:
            print("Value display: enabled; terminal output may include ids, queries, filenames, or addresses.")
    if records:
        print(f"Time range: {fmt_time(records[0].get('ts'))} -> {fmt_time(records[-1].get('ts'))}")
    print("")
    print("What this proves:")
    print("  It shows the SOAP/REST surface a client actually exercised against the bridge.")
    print("  It is evidence for integration discovery, not a compatibility certification.")
    print("")
    print("Trace detail:")
    print("  Values mode is for local developer debugging and correlation between clients.")
    print("  Auth tokens, cookies, raw message bodies, and upload contents are never printed.")
    print("")

    if not records:
        if hidden_noop:
            print("No foreground client-action records found. Use --all to show NoOpRequest heartbeat records.")
        else:
            print("No trace records found. Enable BRIDGE_COMPAT_TRACE_ENABLED=1, restart the bridge, exercise the client, then rerun this command.")
        return

    stats = aggregate(records, show_private=show_private)
    rows: list[list[str]] = []
    for (kind, method, route), item in sorted(
        stats.items(), key=lambda entry: (-entry[1].calls, entry[0])
    ):
        avg_ms, max_ms = fmt_ms(item.durations)
        example = item.shape_examples.most_common(1)[0][0] if item.shape_examples else ""
        status = ",".join(f"{k}:{v}" for k, v in sorted(item.statuses.items()))
        fault_code = item.fault_codes.most_common(1)[0][0] if item.fault_codes else "-"
        rows.append(
            [
                kind,
                method,
                route,
                str(item.calls),
                str(item.faults),
                str(item.private),
                avg_ms,
                max_ms,
                status,
                fault_code,
                one_line(example, 180 if show_private else 100),
            ]
        )

    print("Observed Surface")
    render_table(
        ["KIND", "METHOD", "ROUTE", "CALLS", "FAULTS", "VALUES", "AVG_MS", "MAX_MS", "STATUS", "TOP_FAULT", "FLOW EXAMPLE"],
        rows,
    )
    print("")

    soap_rows = [row for row in rows if row[0] == "SOAP"]
    if soap_rows:
        print("SOAP Method Inventory")
        render_table(["METHOD", "CALLS", "FAULTS", "VALUES", "AVG_MS", "TOP_FAULT"], [[r[1], r[3], r[4], r[5], r[6], r[9]] for r in soap_rows])
        print("")


def render_recent(records: list[dict[str, Any]], tail: int, show_private: bool) -> None:
    if tail <= 0 or not records:
        return
    recent = list(deque(records, maxlen=tail))
    rows: list[list[str]] = []
    for record in recent:
        rows.append(
            [
                fmt_time(record.get("ts")),
                str(record.get("kind") or "?").upper(),
                str(record.get("method") or "?"),
                str(record.get("route") or "?"),
                str(record.get("status") or "?"),
                "yes" if bool_value(record.get("fault")) else "no",
                "yes" if contains_private(record) else "no",
                str(record.get("durationMs") or 0),
                flow_summary(record, 260 if show_private else 140, show_values=show_private),
            ]
        )

    print(f"Recent Calls (last {len(recent)})")
    render_table(["TIME", "KIND", "METHOD", "ROUTE", "STATUS", "FAULT", "VALUES", "MS", "FLOW"], rows)
    print("")
    print("Reading the table:")
    print("  fault=yes means a SOAP body fault or HTTP 4xx/5xx. AUTH_REQUIRED is expected for unauthenticated probes.")
    print("  values=yes means exact developer values are present in the JSONL values block.")
    print("  Values are shown in FLOW by default; use --hide-values for public-shape-only output.")
    print("  query=<custom> and l=<nonNumeric> mean public fields intentionally redacted private/custom values.")


def render_follow(
    path: str,
    include_noop: bool,
    from_start: bool,
    header_every: int,
    show_private: bool,
) -> int:
    headers = ["TIME", "KIND", "METHOD", "ROUTE", "STATUS", "FAULT", "VALUES", "MS", "FLOW"]
    widths = [9, 4, 20, 28, 6, 5, 7, 6, 220 if show_private else 140]
    header = table_line(headers, headers, widths)
    separator = table_line(headers, ["-" * len(h) for h in headers], widths)

    print("Project Z-Bridge Compatibility Trace Follow")
    print("Press Ctrl-C to stop. Default mode hides NoOpRequest; use --all to include heartbeat traffic.")
    if show_private:
        print("Value display is enabled; terminal output may include exact ids, queries, filenames, or addresses.")
    print(header)
    print(separator)
    sys.stdout.flush()

    printed = 0
    try:
        for record in follow_records(path, from_start=from_start):
            if not include_noop and is_noop_record(record):
                continue
            if printed and header_every > 0 and printed % header_every == 0:
                print(header)
                print(separator)
            row = [
                fmt_time(record.get("ts")),
                str(record.get("kind") or "?").upper(),
                str(record.get("method") or "?"),
                str(record.get("route") or "?"),
                str(record.get("status") or "?"),
                "yes" if bool_value(record.get("fault")) else "no",
                "yes" if contains_private(record) else "no",
                str(record.get("durationMs") or 0),
                flow_summary(record, widths[-1], show_values=show_private),
            ]
            print(table_line(headers, row, widths))
            sys.stdout.flush()
            printed += 1
    except KeyboardInterrupt:
        print("")
        print("Stopped compatibility trace follow.")
        return 0

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render or redact Project Z-Bridge compatibility trace JSONL."
    )
    parser.add_argument("paths", nargs="*", help="Trace JSONL path(s), or '-' for stdin.")
    parser.add_argument("--source", default="compat-trace.jsonl", help="Human source label.")
    parser.add_argument("--tail", type=int, default=25, help="Recent call rows to print.")
    parser.add_argument("--foreground", action="store_true", help="Hide NoOpRequest heartbeat records (default).")
    parser.add_argument("--all", action="store_true", help="Include NoOpRequest heartbeat records.")
    parser.add_argument("--follow", action="store_true", help="Follow one trace file and print new records as they arrive.")
    parser.add_argument("--from-start", action="store_true", help="With --follow, print existing records before waiting for new ones.")
    parser.add_argument("--header-every", type=int, default=40, help="With --follow, reprint the table header every N records.")
    parser.add_argument("--summary-only", action="store_true", help="Only print aggregate summary.")
    parser.add_argument("--calls-only", action="store_true", help="Only print recent call table.")
    parser.add_argument("--dump-full", action="store_true", help="Pretty-print full captured request/response blocks.")
    parser.add_argument("--method", help="Filter full dump to one method, e.g. AuthRequest.")
    parser.add_argument("--curl", action="store_true", help="With --dump-full, print curl replay command(s) for captured SOAP request(s).")
    parser.add_argument("--base-url", default=default_curl_base_url(), help="Base URL for --curl replay output.")
    parser.add_argument("--show-private", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hide-values", action="store_true", help="Suppress exact value detail in human output.")
    parser.add_argument("--redact", action="store_true", help="Write JSONL to stdout with private blocks removed.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.redact:
        return redact_records(args.paths)

    if args.follow:
        if len(args.paths) > 1:
            print("compat-trace: --follow accepts at most one trace path", file=sys.stderr)
            return 2
        path = args.paths[0] if args.paths else "-"
        show_values = not args.hide_values
        return render_follow(
            path,
            include_noop=args.all,
            from_start=args.from_start,
            header_every=args.header_every,
            show_private=show_values,
        )

    raw_records, invalid = load_records(args.paths)
    if args.all:
        records = raw_records
        hidden_noop = 0
    else:
        records = [record for record in raw_records if not is_noop_record(record)]
        hidden_noop = len(raw_records) - len(records)
    show_values = (args.show_private or any(contains_private(record) for record in records)) and not args.hide_values

    if args.dump_full:
        render_full_dump(records, args.tail, args.method, args.curl, args.base_url)
        return 0

    if not args.calls_only:
        render_summary(records, invalid, args.source, hidden_noop, show_private=show_values)
    if not args.summary_only:
        if not args.calls_only:
            print("")
        render_recent(records, args.tail, show_private=show_values)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
