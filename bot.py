"""Poormaz News Bot entry point (used by .github/workflows/newsbot.yml).

News Bot 2.0 lives in the newsbot/ package. The previous bot is preserved unchanged
in legacy/bot_v1.py for immediate rollback (set the repository variable
NEWSBOT_ENGINE=legacy; see docs/ROLLBACK.md).
"""

import sys

from newsbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
