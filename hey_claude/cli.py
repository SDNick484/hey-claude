"""Command-line entry point: `hey-claude <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

from hey_claude import __version__, config, control, feedback, history, models

log = logging.getLogger("hey_claude")


def _load(args) -> config.Config:
    try:
        return config.load(Path(args.config) if args.config else None)
    except (ValueError, OSError) as e:
        raise SystemExit(str(e)) from None


# -- daemon --------------------------------------------------------------------

def cmd_run(args) -> int:
    from hey_claude.app import Daemon

    cfg = _load(args)
    try:
        Daemon(cfg, dry_run=args.dry_run).run()
    except RuntimeError as e:
        raise SystemExit(str(e)) from None
    return 0


def cmd_setup(args) -> int:
    todo = list(models.ALL) if args.force else models.missing()
    if not todo:
        print(f"All models present in {config.models_dir()}")
        return 0
    total = sum(m.approx_mb for m in todo)
    print(f"Downloading {len(todo)} model(s), about {total} MB, to {config.models_dir()}")
    for spec in todo:
        print(f"- {spec.key}: {spec.url.rsplit('/', 1)[1]}")
        models.fetch(spec)
    print("Done. Next: `hey-claude doctor`, then `hey-claude calibrate`.")
    return 0


def cmd_doctor(args) -> int:
    from hey_claude import hotkey
    from hey_claude.launcher import Launcher, detect_terminal

    cfg = _load(args)
    bad = 0

    def line(ok: bool | None, what: str, hint: str = "") -> None:
        nonlocal bad
        mark = {True: "ok ", False: "!! ", None: "-- "}[ok]
        bad += ok is False
        print(f"  {mark} {what}" + (f"\n        {hint}" if hint else ""))

    print(f"hey-claude {__version__}")
    print(f"config: {config.config_path()}" + ("" if config.config_path().exists() else " (defaults)"))
    for spec in models.ALL:
        line(models.installed(spec), f"model {spec.key}", "" if models.installed(spec) else "run `hey-claude setup`")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        line(True, f"microphone: {dev['name']}")
    except Exception as e:  # PortAudio missing or no input device
        line(False, "microphone", f"{e} (Gentoo: emerge media-libs/portaudio)")
    launcher = Launcher(cfg.launch, cfg.projects)
    problems = launcher.problems()
    line(not problems, f"launch mode {cfg.launch.mode!r}"
         + (f", terminal {cfg.launch.terminal if cfg.launch.terminal != 'auto' else detect_terminal()}"
            if cfg.launch.mode != "command" else ""), "; ".join(problems))
    for name, path in cfg.projects.items():
        line(config.expand(path).is_dir(), f"project {name!r} -> {path}")
    ok, why = hotkey.available()
    line(ok if cfg.ptt.backend == "evdev" else (True if ok else None),
         f"push-to-talk ({cfg.ptt.backend}, {cfg.ptt.key})" if ok else "push-to-talk via evdev",
         why + ("; or bind `hey-claude ptt start/stop` in your compositor" if why else ""))
    line(True if shutil.which("notify-send") else None, "notify-send",
         "" if shutil.which("notify-send") else "optional (Gentoo: emerge x11-libs/libnotify)")
    try:
        line(True, f"daemon running: {_describe(control.request('status')['status'])}")
    except OSError:
        line(None, "daemon not running")
    return 1 if bad else 0


def cmd_calibrate(args) -> int:
    from hey_claude import calibrate

    return calibrate.run(_load(args), samples=args.samples, reuse=args.reuse, write=not args.no_write,
                         config_path=Path(args.config) if args.config else None)


def cmd_simulate(args) -> int:
    import shlex

    from hey_claude import app, audio

    cfg = _load(args)
    samples = audio.read_wav(Path(args.wav))
    presses = []
    for spec in args.ptt or []:
        start, _, end = spec.partition(":")
        presses.append((float(start), float(end)))
    results = app.simulate(cfg, samples, ptt=presses)
    for when, launch, commands in results:
        req = launch.request
        print(f"{when:6.2f}s  [{launch.source.value}] heard {launch.text!r}")
        print(f"         prompt={req.prompt!r} project={req.project!r}")
        for argv in commands:
            print(f"         $ {shlex.join(argv)}")
    if not results:
        print("no launches")
    return 0


def cmd_launch(args) -> int:
    import shlex

    from hey_claude.launcher import Launcher
    from hey_claude.transcript import Request, parse

    cfg = _load(args)
    if args.project and args.project.lower() not in cfg.projects:
        raise SystemExit(f"unknown project {args.project!r}; known: {', '.join(cfg.projects) or 'none'}")
    text = " ".join(args.prompt)
    req = parse(text, phrases=cfg.wake.phrases, projects=cfg.projects) if args.parse else Request(text)
    if args.project:
        req = Request(req.prompt, args.project.lower())
    outcome = Launcher(cfg.launch, cfg.projects, dry_run=args.dry_run).launch(req)
    print(outcome.summary)
    if args.dry_run:
        for argv in outcome.commands:
            print(f"$ {shlex.join(argv)}")
    return 0


# -- control -------------------------------------------------------------------

def _send(cmd: str) -> dict:
    try:
        return control.request(cmd)
    except OSError:
        raise SystemExit("hey-claude is not running (start it with `hey-claude run`)") from None


def _describe(st: dict) -> str:
    state = st.get("state", "stopped")
    # While capturing via push-to-talk the state isn't "muted", so say so separately.
    return state + (" (wake word muted)" if st.get("muted") and state != "muted" else "")


def cmd_simple(args) -> int:
    print(_describe(_send(args.control)["status"]))
    return 0


def cmd_ptt(args) -> int:
    _send(f"ptt-{args.action}")
    return 0


def cmd_status(args) -> int:
    def show(st: dict) -> None:
        if args.waybar:
            print(json.dumps(feedback.waybar(st)), flush=True)
        elif args.json:
            print(json.dumps(st), flush=True)
        else:
            print(_describe(st), flush=True)

    if not args.watch:
        try:
            show(control.request("status")["status"])
        except OSError:
            show({"state": "stopped", "muted": False})
        return 0
    # Keep running for bars: reconnect whenever the daemon restarts.
    while True:
        try:
            for st in control.watch():
                show(st)
        except OSError:
            pass
        show({"state": "stopped", "muted": False})
        time.sleep(2)


def cmd_history(args) -> int:
    for e in history.recent(args.n):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("time", 0)))
        where = e.get("project") or e.get("workdir", "")
        print(f"{when}  {e.get('source', '?'):4}  {where}  {e.get('prompt') or '(new session)'}")
    return 0


def cmd_devices(_args) -> int:
    from hey_claude import audio

    print(audio.list_devices())
    return 0


def cmd_config(args) -> int:
    path = Path(args.config) if args.config else config.config_path()
    if args.init:
        if path.exists():
            raise SystemExit(f"{path} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resources.files("hey_claude").joinpath("config.example.toml").read_text())
        print(f"Wrote {path}")
        return 0
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hey-claude", description="Voice-activated launcher for Claude Code.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", help="config file (default: $XDG_CONFIG_HOME/hey-claude/config.toml)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="run the listener (foreground)")
    s.add_argument("--dry-run", action="store_true", help="log launches instead of running them")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("setup", help="download the speech models (~500 MB)")
    s.add_argument("--force", action="store_true", help="re-download even if present")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check models, audio, launcher and permissions").set_defaults(func=cmd_doctor)

    s = sub.add_parser("calibrate", help="tune the wake word to your voice and mic")
    s.add_argument("--samples", type=int, default=5)
    s.add_argument("--reuse", action="store_true", help="re-evaluate the last recordings")
    s.add_argument("--no-write", action="store_true", help="don't update the config file")
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("simulate", help="run the pipeline over a WAV file (no launches)")
    s.add_argument("wav")
    s.add_argument("--ptt", action="append", metavar="START:END",
                   help="simulate holding push-to-talk between these times (seconds)")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("launch", help="launch Claude Code as if spoken (tests the launcher)")
    s.add_argument("prompt", nargs="*")
    s.add_argument("-p", "--project")
    s.add_argument("--parse", action="store_true", help="treat the text as a transcript (wake word, \"in <project>\")")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_launch)

    for name, help_ in [("mute", "stop listening for the wake word"), ("unmute", "resume the wake word"),
                        ("toggle-mute", "toggle the wake word"), ("cancel", "abandon the current utterance")]:
        sub.add_parser(name, help=help_).set_defaults(func=cmd_simple, control=name)

    s = sub.add_parser("ptt", help="push-to-talk control for compositor key bindings")
    s.add_argument("action", choices=["start", "stop", "toggle"])
    s.set_defaults(func=cmd_ptt)

    s = sub.add_parser("status", help="show the daemon state")
    s.add_argument("--watch", action="store_true", help="keep printing on every change")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true")
    g.add_argument("--waybar", action="store_true", help="Waybar custom-module JSON")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("history", help="recent launches")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_history)

    sub.add_parser("devices", help="list audio devices").set_defaults(func=cmd_devices)

    s = sub.add_parser("config", help="print the config path, or --init to write an example")
    s.add_argument("--init", action="store_true")
    s.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.INFO if args.command in ("run", "simulate") else logging.WARNING
    if args.verbose:
        level = logging.DEBUG if args.verbose > 1 else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
