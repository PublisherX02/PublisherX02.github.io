import sys
from pathlib import Path
import importlib.util

SRC_FILE = Path(__file__).resolve().parent / "src" / "run_agent.py"
spec = importlib.util.spec_from_file_location("src_run_agent", SRC_FILE)
module = importlib.util.module_from_spec(spec)
sys.modules["run_agent"] = module
spec.loader.exec_module(module)

def main():
    return module.main()

if __name__ == "__main__":
    main()
