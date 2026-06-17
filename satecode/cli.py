import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from satecode import __version__


def cmd_patch(args: argparse.Namespace) -> int:
    from satecode.runtime import ImagePatcher
    try:
        p   = ImagePatcher(args.input, dockerfile=args.dockerfile)
        out = p.patch(args.output, force=args.force, tag_suffix=args.tag_suffix)
        print("done: {}".format(out))
        return 0
    except (FileNotFoundError, FileExistsError) as e:
        print(str(e), file=sys.stderr); return 1
    except Exception as e:
        print(str(e), file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        return 1


def cmd_info(args: argparse.Namespace) -> int:
    from satecode.runtime import ImagePatcher
    try:
        p      = ImagePatcher(args.input)
        ep, cm = p.get_original_entrypoint()
        print("image:     {}".format(args.input))
        print("cmd:       {}".format(ep + cm))
        print("patched:   {}".format(p.is_patched()))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr); return 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    from satecode.monitor import SnapshotCoordinator
    try:
        sc = SnapshotCoordinator(
            task_id=args.task_id,
            output_dir=Path(args.output_dir),
            token_path=args.token_path,
            runtime=args.runtime,
            namespace=args.namespace,
            ready_timeout=args.ready_timeout,
            exec_timeout=args.exec_timeout,
        )
        r = sc.run(resume=not args.no_resume)
        print("bundle: {}".format(r["bundle_path"]))
        print("digest: {}".format(r["digest"]))
        if args.json:
            print(json.dumps({k: str(v) for k, v in r.items()}, indent=2))
        return 0
    except (TimeoutError, RuntimeError) as e:
        print(str(e), file=sys.stderr); return 1
    except Exception as e:
        print(str(e), file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        return 1


def cmd_record(args: argparse.Namespace) -> int:
    from satecode.monitor import record_access_sequence, record_from_log
    try:
        rootfs = Path(args.rootfs)
        if args.log:
            tier_a, tier_b = record_from_log(Path(args.log), rootfs)
        else:
            if not args.restore_cmd:
                print("provide --restore-cmd or --log", file=sys.stderr); return 1
            tier_a, tier_b = record_access_sequence(args.restore_cmd, rootfs,
                                                    strace_bin=args.strace)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as fh:
            for p in tier_a: fh.write(p + "\n")
            fh.write("---\n")
            for p in tier_b: fh.write(p + "\n")
        print("tier_a: {}  tier_b: {}  -> {}".format(len(tier_a), len(tier_b), out))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        return 1


def cmd_build(args: argparse.Namespace) -> int:
    from satecode.runtime import build_image
    try:
        seq = Path(args.seq)
        tier_a, tier_b, section = [], [], 0
        for line in seq.read_text().splitlines():
            if line == "---": section = 1; continue
            (tier_a if section == 0 else tier_b).append(line)
        aux = Path(args.aux_dir) if args.aux_dir else None
        boundary = build_image(
            image_tar=Path(args.image),
            output=Path(args.output),
            tier_a=tier_a, tier_b=tier_b,
            aux_dir=aux,
            mkfs_bin=args.mkfs_erofs,
        )
        meta = {
            "output": str(Path(args.output).resolve()),
            "boundary": boundary,
            "tier_a_count": len(tier_a),
            "tier_b_count": len(tier_b),
        }
        meta_path = Path(args.output).with_suffix(".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2))
        print("output:   {}".format(args.output))
        print("meta:     {}".format(meta_path))
        print("boundary: {}".format(boundary))
        print("pass --meta {} to emit".format(meta_path))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        return 1


def cmd_emit(args: argparse.Namespace) -> int:
    from satecode.codegen import emit
    try:
        image_path = args.image_path
        boundary   = args.boundary

        if args.meta:
            meta = json.loads(Path(args.meta).read_text())
            if image_path is None:
                image_path = meta.get("output")
            if boundary is None:
                boundary = meta.get("boundary")

        if image_path is None:
            print("--image-path required (or pass --meta)", file=sys.stderr)
            return 1
        if boundary is None:
            print("--boundary required (or pass --meta)", file=sys.stderr)
            return 1

        artifacts = emit(
            output_dir=Path(args.output_dir),
            image_path=image_path,
            boundary=boundary,
            arch=args.arch,
            compile_binary=not args.no_compile,
            exec_path=args.exec_path,
        )
        for k, v in artifacts.items():
            print("{}: {}".format(k, v))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        return 1



def create_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="satecode")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("patch")
    s.add_argument("-i", "--input",  required=True)
    s.add_argument("-o", "--output", required=True)
    s.add_argument("-d", "--dockerfile")
    s.add_argument("-f", "--force",  action="store_true")
    s.add_argument("-s", "--tag-suffix", default="-wrapped", dest="tag_suffix")
    s.set_defaults(func=cmd_patch)

    s = sub.add_parser("info")
    s.add_argument("-i", "--input", required=True)
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("snapshot")
    s.add_argument("task_id")
    s.add_argument("-o", "--output-dir", required=True, dest="output_dir")
    s.add_argument("--token-path",    default="/tmp/checkpoint_ready", dest="token_path")
    s.add_argument("--runtime",       default="ctr")
    s.add_argument("--namespace",     default="default")
    s.add_argument("--ready-timeout", type=float, default=600.0, dest="ready_timeout")
    s.add_argument("--exec-timeout",  type=float, default=120.0, dest="exec_timeout")
    s.add_argument("--no-resume",     action="store_true", dest="no_resume")
    s.add_argument("--json",          action="store_true")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("record")
    s.add_argument("--rootfs",      required=True)
    s.add_argument("--restore-cmd", nargs=argparse.REMAINDER, dest="restore_cmd")
    s.add_argument("--log")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--strace",       default="strace")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("build")
    s.add_argument("-i", "--image",    required=True)
    s.add_argument("-o", "--output",   required=True)
    s.add_argument("-s", "--seq",      required=True)
    s.add_argument("-a", "--aux-dir",  dest="aux_dir")
    s.add_argument("--mkfs-erofs",     default="mkfs.erofs", dest="mkfs_erofs")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("emit")
    s.add_argument("-o", "--output-dir", required=True, dest="output_dir")
    s.add_argument("--image-path",       default=None,  dest="image_path")
    s.add_argument("--boundary",         type=int, default=None)
    s.add_argument("--meta",             default=None,
                   help="path to .meta.json written by build (alternative to "
                        "--image-path + --boundary)")
    s.add_argument("--arch", default="native",
                   choices=["native", "aarch64", "arm", "x86_64"])
    s.add_argument("--no-compile",  action="store_true", dest="no_compile")
    s.add_argument("--exec-path",   default="/usr/local/bin/sat_primer", dest="exec_path")
    s.set_defaults(func=cmd_emit)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = create_parser()
    args   = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(); return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
