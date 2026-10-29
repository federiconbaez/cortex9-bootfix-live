"""Runner de tests: falla si algún test se saltea (un skip silencioso cuenta como falla)."""

import sys
import unittest


def main() -> int:
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"tests: {res.testsRun} corridos, {len(res.failures)} fallas, {len(res.errors)} errores, "
          f"{len(res.skipped)} salteados")
    if res.skipped:
        print("FALLA: hay tests salteados")
        return 1
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
