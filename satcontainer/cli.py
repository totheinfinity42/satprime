"""统一CLI入口"""

import argparse
import sys
from typing import Optional

from satcontainer import __version__


def cmd_inject(args: argparse.Namespace) -> int:
    """执行镜像注入命令"""
    from satcontainer.injector import ImageInjector

    try:
        injector = ImageInjector(
            input_tar=args.input,
            dockerfile=args.dockerfile,
        )

        output_tar = injector.inject(
            output_tar=args.output,
            force=args.force,
            tag_suffix=args.tag_suffix,
        )

        print(f"Successfully injected wrapper into image: {output_tar}")
        return 0

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def cmd_inspect(args: argparse.Namespace) -> int:
    """检查镜像的注入状态"""
    from satcontainer.injector import ImageInjector

    try:
        injector = ImageInjector(args.input)
        entrypoint, cmd = injector.get_original_entrypoint()

        print(f"Image: {args.input}")
        print(f"ENTRYPOINT: {entrypoint}")
        print(f"CMD: {cmd}")

        if injector.is_injected():
            print("Status: Already injected with SatContainer wrapper")
        else:
            print("Status: Not injected")

        return 0

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def create_parser() -> argparse.ArgumentParser:
    """创建命令行解析器"""
    parser = argparse.ArgumentParser(
        prog="satcontainer",
        description="卫星容器启动优化框架",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # inject 命令
    inject_parser = subparsers.add_parser(
        "inject",
        help="将checkpoint wrapper注入到OCI镜像tar文件",
    )
    inject_parser.add_argument(
        "-i", "--input",
        required=True,
        help="输入OCI镜像tar文件路径（如 demo_apps/mmrotate/mmrotate.tar）",
    )
    inject_parser.add_argument(
        "-o", "--output",
        required=True,
        help="输出tar文件路径",
    )
    inject_parser.add_argument(
        "-d", "--dockerfile",
        help="可选的Dockerfile路径（用于辅助分析入口点）",
    )
    inject_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="强制覆盖已存在的输出文件",
    )
    inject_parser.add_argument(
        "-s", "--tag-suffix",
        default="-wrapped",
        help="镜像标签后缀（默认 '-wrapped'），设为空字符串保持原名",
    )
    inject_parser.set_defaults(func=cmd_inject)

    # inspect 命令
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="检查镜像tar文件的注入状态和入口点信息",
    )
    inspect_parser.add_argument(
        "-i", "--input",
        required=True,
        help="要检查的镜像tar文件路径",
    )
    inspect_parser.set_defaults(func=cmd_inspect)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI主入口"""
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
