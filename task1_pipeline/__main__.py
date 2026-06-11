from __future__ import annotations

from .config import build_arg_parser, load_config
from .pipeline import BatchPipeline


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = load_config(args.config, args)
    pipeline = BatchPipeline(config=config, limit=args.limit, limit_per_type=args.limit_per_type)
    pipeline.run()


if __name__ == "__main__":
    main()
