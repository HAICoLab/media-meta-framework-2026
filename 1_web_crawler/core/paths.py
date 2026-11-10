"""프로젝트 루트 기준 경로. DB·위키 덤프·data 디렉터리 위치의 단일 출처."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "dramas.db"
WIKI_DUMP = ROOT / "kowiki-articles.xml.bz2"
WIKI_INDEX = ROOT / "wiki_dump_index.db"
DATA_DIR = ROOT / "data"
