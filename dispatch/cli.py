"""Headless entry point: every GUI action also works here.

    dispatch validate --config campaign.yaml
    dispatch send --config campaign.yaml [--dry-run] [--quota N] [--yes]
    dispatch export --config campaign.yaml --out results.csv
    dispatch sync-inbox --config campaign.yaml
    dispatch template new|preview|validate --config campaign.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, db
from .config import ConfigError, FullConfig, load_config, write_config
from .engine import preflight as preflight_mod
from .engine.worker import Worker
from .mail import blocks, compose
from .mail.secrets import SecretNotFound, resolve_secret
from .sheet import writer as sheet_writer

STARTERS = Path(__file__).parent / "templates" / "starters"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def load(path: str) -> FullConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _conn(cfg: FullConfig):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    return conn


def _print_report(report: preflight_mod.PreflightReport) -> None:
    print(report.headline())
    for line in report.lines():
        print("  " + line)


def _event_printer(quota: int):
    state = {"n": 0}

    def handle(event: dict) -> None:
        kind = event.get("type")
        if kind == "sent":
            state["n"] += 1
            tag = "dry-run" if event.get("dry_run") else "sent"
            print(f"  [{state['n']}/{quota}] {tag} {event.get('email')}", flush=True)
        elif kind == "failed":
            print(f"  !! {event.get('state')} {event.get('email')}: "
                  f"{event.get('error')}", flush=True)
        elif kind == "log":
            print(f"  {event.get('level')}: {event.get('message')}", flush=True)
        elif kind == "countdown" and event.get("seconds") in (60, 30, 10, 5):
            print(f"  ... next send in {event['seconds']}s", flush=True)
        elif kind == "window":
            print(f"  waiting for the send window: {event.get('opens_at')}", flush=True)
        elif kind == "backoff":
            print(f"  backing off for {event.get('seconds')}s", flush=True)
        elif kind == "suspended":
            print(f"  account suspended until {event.get('until')}", flush=True)

    return handle


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_validate(args) -> int:
    cfg = load(args.config)
    conn = _conn(cfg)
    try:
        report = preflight_mod.run_preflight(cfg, conn, quota=args.quota)
        _print_report(report)
        return 0 if report.ok else 1
    finally:
        conn.close()


def cmd_send(args) -> int:
    cfg = load(args.config)
    dry = True if args.dry_run else cfg.safety.dry_run
    conn = _conn(cfg)
    try:
        report = preflight_mod.run_preflight(cfg, conn, quota=args.quota)
        _print_report(report)
        if not report.ok:
            print("\nRun blocked. Fix the issues above (or --quota to reduce).", file=sys.stderr)
            return 1
        planned = min(report.planned, args.quota or report.planned)
        if not dry:
            needed = f"SEND {planned}"
            if planned > cfg.safety.require_typed_confirmation_over:
                if args.yes or not sys.stdin.isatty():
                    if not args.yes:
                        print(f"\nRefusing to send {planned} live without confirmation. "
                              f"Re-run with --yes.", file=sys.stderr)
                        return 1
                else:
                    answer = input(f"\nThis will send {planned} real emails. "
                                   f"Type '{needed}' to continue: ").strip()
                    if answer != needed:
                        print("Cancelled.", file=sys.stderr)
                        return 1
        print(f"\n{'DRY RUN — writing .eml files only' if dry else 'LIVE SEND'} "
              f"to {cfg.sender.from_email}, quota {planned}\n")
    finally:
        conn.close()

    worker = Worker(cfg, quota=planned, dry_run=dry, on_event=_event_printer(planned),
                    prepared=True)
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()
        print("\nStopping after the current message...")
    print(f"\nFinished: {worker.stats.sent} sent, {worker.stats.failed} failed, "
          f"reason: {worker.stop_reason}")
    return 0


def cmd_export(args) -> int:
    cfg = load(args.config)
    conn = _conn(cfg)
    try:
        run_id = args.run_id or _latest_run_id(conn)
        if not run_id:
            print("no runs recorded yet", file=sys.stderr)
            return 1
        rows = conn.execute(
            """SELECT c.sheet_row, c.first_name, c.last_name, c.company, c.email,
                      c.last_advertised, c.replied, s.state, s.sent_at, s.smtp_code,
                      s.error, s.message_id, s.dry_run
               FROM sends s LEFT JOIN contacts c ON c.id=s.contact_id
               WHERE s.run_id=? ORDER BY s.id""", (run_id,),
        ).fetchall()
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sheet_row", "first_name", "last_name", "company", "email",
                        "last_advertised", "replied", "state", "sent_at", "smtp_code",
                        "error", "message_id", "dry_run"])
            for r in rows:
                w.writerow([r[k] for k in r.keys()])
        print(f"exported {len(rows)} row(s) to {out}")
        return 0
    finally:
        conn.close()


def _latest_run_id(conn) -> int | None:
    row = db.latest_run(conn)
    return int(row["id"]) if row else None


def cmd_sync_inbox(args) -> int:
    cfg = load(args.config)
    from .mail.monitor import poll_inbox
    conn = _conn(cfg)
    try:
        counts = poll_inbox(cfg, conn)
        print("inbox check complete: " +
              (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing new"))
        db.set_meta(conn, f"imap_checked:{cfg.sender.id}", db.now_iso())
        return 0
    except SecretNotFound as exc:
        print(f"cannot check the inbox: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"IMAP error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_flush(args) -> int:
    cfg = load(args.config)
    conn = _conn(cfg)
    try:
        run_id = args.run_id or _latest_run_id(conn)
        updates = sheet_writer.collect_updates(conn, run_id)
        changes = sheet_writer.plan(cfg.sheet_file, updates, cfg.campaign.sheet_tab)
        for change in changes[:200]:
            print("  " + change.describe())
        if not changes:
            print("nothing to write")
            return 0
        if args.dry_run:
            print(f"{len(changes)} change(s) would be written (--dry-run)")
            return 0
        result = sheet_writer.write_back(cfg.sheet_file, updates, cfg.campaign.sheet_tab)
        print(result.summary + f" -> {result.path}")
        return 0
    except sheet_writer.SheetLockedError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_init(args) -> int:
    from .config import default_config
    target = Path(args.dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_path = target / "campaign.yaml"
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    cfg = default_config(target)
    write_config(cfg, cfg_path)
    _install_starter(cfg, "promo_card", "promo")
    sample = Path(__file__).resolve().parent.parent / "sample_leads.xlsx"
    sheet = target / "leads.xlsx"
    if sample.exists() and not sheet.exists():
        shutil.copy2(sample, sheet)
        print(f"copied a 12-row sample sheet to {sheet}")
    print(f"created {cfg_path}")
    print("Next: edit campaign.yaml (sender.smtp.host, imap.host + password in Settings),")
    print("then run `dispatch validate --config campaign.yaml`.")
    return 0


# --------------------------------------------------------------------------
# template commands
# --------------------------------------------------------------------------
def _templates_dir(cfg: FullConfig) -> Path:
    return cfg.html_file.parent


def _install_starter(cfg: FullConfig, starter: str, name: str) -> Path:
    source = STARTERS / f"{starter}.json"
    if not source.exists():
        raise SystemExit(f"unknown starter {starter!r} "
                         f"(choose from {', '.join(p.stem for p in STARTERS.glob('*.json'))})")
    doc = blocks.load_document(source)
    doc["name"] = name.replace("_", " ").title()
    path = _templates_dir(cfg) / f"{name}.json"
    blocks.save_document(doc, path)
    cfg.template.kind = "blocks"
    cfg.template.blocks_path = str(path)
    cfg.template.html_path = str(path.with_suffix(".html"))
    cfg.template.text_path = str(path.with_suffix(".txt"))
    _write_artifacts(cfg, doc)
    if cfg.config_path:
        write_config(cfg, cfg.config_path)
    return path


def _write_artifacts(cfg: FullConfig, doc: dict) -> tuple[Path, Path]:
    html = blocks.render_document_html(doc)
    text = blocks.render_document_text(doc)
    cfg.html_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.html_file.write_text(html, encoding="utf-8")
    cfg.text_file.write_text(text, encoding="utf-8")
    cfg.template.subject = str(doc.get("subject") or cfg.template.subject)
    return cfg.html_file, cfg.text_file


def cmd_template(args) -> int:
    cfg = load(args.config)
    if args.template_cmd == "list":
        for p in sorted(STARTERS.glob("*.json")):
            print(p.stem)
        return 0

    if args.template_cmd == "new":
        name = args.name or args.starter or "personal_note"
        path = _install_starter(cfg, args.starter or "personal_note", name)
        print(f"created {path} (and .html/.txt)")
        return 0

    # preview / validate operate on the current template
    if cfg.template_kind() == "blocks":
        doc = blocks.load_document(cfg.blocks_file)
        if args.template_cmd == "validate":
            columns = _sheet_columns(cfg)
            errors, warnings = blocks.validate_document(doc, columns)
            for warning in warnings:
                print(f"warning: {warning}")
            for error in errors:
                print(f"error: {error}")
            print("valid" if not errors else f"{len(errors)} error(s)")
            return 0 if not errors else 1
        html_path, text_path = _write_artifacts(cfg, doc)
    else:
        if args.template_cmd == "validate":
            try:
                compose.render_html(cfg, compose.template_values(cfg, _sample_contact()))
                compose.render_text(cfg, compose.template_values(cfg, _sample_contact()))
                print("valid")
                return 0
            except Exception as exc:
                print(f"error: {exc}")
                return 1
        values = compose.template_values(cfg, _sample_contact())
        html_path, text_path = compose.write_preview(cfg, values)

    out_html, out_text = compose.write_preview(
        cfg, compose.template_values(cfg, _sample_contact())
    )
    print(f"preview written:\n  {out_html}\n  {out_text}")
    return 0


def _sample_contact():
    return {
        "email": "john@example.com",
        "first_name": "John",
        "last_name": "Smith",
        "company": "Acme Ltd",
    }


def _sheet_columns(cfg: FullConfig) -> list[str]:
    try:
        from .sheet.reader import read_sheet
        data = read_sheet(cfg.sheet_file, cfg.campaign.sheet_tab, cfg.campaign.date_order)
        return data.headers
    except Exception:
        return []


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"dispatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p):
        p.add_argument("--config", default="campaign.yaml", help="path to campaign.yaml")

    p_val = sub.add_parser("validate", help="run the pre-flight report and exit 0/1")
    add_config(p_val)
    p_val.add_argument("--quota", type=int)
    p_val.set_defaults(func=cmd_validate)

    p_send = sub.add_parser("send", help="import, queue and send")
    add_config(p_send)
    p_send.add_argument("--dry-run", action="store_true", help="force dry run")
    p_send.add_argument("--quota", type=int)
    p_send.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation (non-interactive live runs)")
    p_send.set_defaults(func=cmd_send)

    p_exp = sub.add_parser("export", help="export a run to CSV")
    add_config(p_exp)
    p_exp.add_argument("--out", default="results.csv")
    p_exp.add_argument("--run-id", type=int, dest="run_id")
    p_exp.set_defaults(func=cmd_export)

    p_imap = sub.add_parser("sync-inbox", help="run one IMAP pass")
    add_config(p_imap)
    p_imap.set_defaults(func=cmd_sync_inbox)

    p_flush = sub.add_parser("flush", help="write sent results back to the spreadsheet")
    add_config(p_flush)
    p_flush.add_argument("--run-id", type=int, dest="run_id")
    p_flush.add_argument("--dry-run", action="store_true")
    p_flush.set_defaults(func=cmd_flush)

    p_init = sub.add_parser("init", help="create a campaign.yaml + starter template")
    p_init.add_argument("--dir", default=".")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_tpl = sub.add_parser("template", help="template new | preview | validate | list")
    p_tpl.add_argument("template_cmd", choices=["new", "preview", "validate", "list"])
    add_config(p_tpl)
    p_tpl.add_argument("--starter", default="personal_note",
                       choices=["personal_note", "promo_card", "announcement"])
    p_tpl.add_argument("--name")
    p_tpl.set_defaults(func=cmd_template)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except SecretNotFound as exc:
        print(f"password error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
