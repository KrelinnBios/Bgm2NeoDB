from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HOST = "127.0.0.1"
PORT = 8765
ORIGIN = f"http://{HOST}:{PORT}"
CALLBACK = f"{ORIGIN}/auth/neodb/callback"
USER_AGENT = (
    "KrelinnBios/Bgm2NeoDB/0.1.0 (https://github.com/KrelinnBios/Bgm2NeoDB; built with Codex)"
)
BANGUMI_BASE = "https://api.bgm.tv"
BANGUMI_TOKEN_URL = "https://next.bgm.tv/demo/access-token"
NEODB_DEFAULT = "https://neodb.social"
