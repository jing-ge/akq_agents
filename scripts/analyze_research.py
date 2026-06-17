from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from akq_agents.cli.app import main


if __name__ == "__main__":
    sys.argv = [sys.argv[0], "analyze"]
    main()
