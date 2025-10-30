import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from utils.crcon_http import CRCONHttpClient, CRCONHTTPError


def main() -> int:
    try:
        client = CRCONHttpClient.from_env()
    except CRCONHTTPError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        maps_response = client.get_maps()
    except CRCONHTTPError as exc:
        print(f"Request failed: {exc}")
        return 1

    print("Successfully fetched maps response:")
    print(json.dumps(maps_response, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
