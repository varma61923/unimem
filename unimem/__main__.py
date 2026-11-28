import sys

from unimem.cli import main

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "__internal_warm":
        from unimem.warm import main as warm_main

        raise SystemExit(warm_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "__internal_hook_bridge":
        from unimem.hook_bridge import main as bridge_main

        raise SystemExit(bridge_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "__internal_hook_kit":
        from unimem.hook_kit import main as hook_kit_main

        raise SystemExit(hook_kit_main(sys.argv[2:]))
    raise SystemExit(main())
