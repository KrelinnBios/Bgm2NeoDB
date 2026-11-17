import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def profile_id(bangumi_id, instance, neodb_url):
    return hashlib.sha256(encode([bangumi_id, instance, neodb_url]).encode()).hexdigest()[:24]


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, scan TEXT,
                    export_complete INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS entries (
                    profile TEXT NOT NULL, subject_id INTEGER NOT NULL,
                    source TEXT NOT NULL, scan TEXT NOT NULL, item TEXT, plan TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', stage TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    migrated_at TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile, subject_id));
            """)
            if "migrated_fields" not in {
                r["name"] for r in db.execute("PRAGMA table_info(entries)")
            }:
                db.execute(
                    "ALTER TABLE entries ADD COLUMN migrated_fields TEXT NOT NULL DEFAULT '[]'"
                )
            columns = {r["name"] for r in db.execute("PRAGMA table_info(entries)")}
            for column in ("resolution", "subject_detail"):
                if column not in columns:
                    db.execute(f"ALTER TABLE entries ADD COLUMN {column} TEXT")
            db.execute(
                """CREATE INDEX IF NOT EXISTS entries_migrated_target
                ON entries(profile, json_extract(item, '$.uuid')) WHERE status='migrated'"""
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def profile(self, pid, metadata=None):
        with self.connect() as db:
            if metadata is not None:
                db.execute(
                    """INSERT INTO profiles(id,metadata) VALUES(?,?)
                    ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata
                    WHERE profiles.metadata<>excluded.metadata""",
                    (pid, encode(metadata)),
                )
            row = db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            return dict(row) if row else None

    def import_snapshot(self, pid, scan, sources):
        with self.connect() as db:
            for source in sources:
                source_json = encode(source)
                # 检查条目是否已存在且已完成迁移
                existing = db.execute(
                    "SELECT source, status FROM entries WHERE profile=? AND subject_id=?",
                    (pid, source["subject_id"]),
                ).fetchone()

                if (
                    existing
                    and existing["status"] == "migrated"
                    and existing["source"] == source_json
                ):
                    # 已完成且数据未变化，仅更新scan标记，保持migrated状态
                    db.execute(
                        "UPDATE entries SET scan=?, updated_at=? WHERE profile=? AND subject_id=?",
                        (scan, now(), pid, source["subject_id"]),
                    )
                else:
                    # 新条目或数据有变化，按原逻辑处理
                    db.execute(
                        """INSERT INTO entries(profile,subject_id,source,scan,updated_at)
                        VALUES(?,?,?,?,?) ON CONFLICT(profile,subject_id) DO UPDATE SET
                        source=excluded.source, scan=excluded.scan, plan=NULL,
                        status='pending',stage='',error='',subject_detail=NULL,
                        resolution=CASE WHEN json_extract(entries.resolution,'$.basis')='manual'
                        THEN entries.resolution ELSE NULL END,updated_at=excluded.updated_at""",
                        (pid, source["subject_id"], source_json, scan, now()),
                    )
            db.execute("UPDATE profiles SET scan=?,export_complete=1 WHERE id=?", (scan, pid))

    def rows(self, pid, *, subject_id=None):
        condition = " AND e.subject_id=?" if subject_id is not None else ""
        parameters = (pid, subject_id) if subject_id is not None else (pid,)
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.* FROM entries e JOIN profiles p ON p.id=e.profile
                WHERE e.profile=? AND e.scan=p.scan"""
                + condition
                + " ORDER BY e.subject_id",
                parameters,
            ).fetchall()
        return self._decode_rows(rows)

    def page(self, pid, *, page=1, page_size=30, statuses=None, query=""):
        conditions = ["e.profile=?", "e.scan=p.scan"]
        parameters = [pid]
        if statuses is not None:
            if not statuses:
                return 0, []
            conditions.append("e.status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        if query:
            # Keep Python's Unicode casefold and literal substring semantics.
            conditions.append("""instr(casefold(COALESCE(
                NULLIF(json_extract(e.source, '$.subject.name_cn'), ''),
                NULLIF(json_extract(e.source, '$.subject.name'), ''),
                CAST(e.subject_id AS TEXT)) || e.subject_id), ?) > 0""")
            parameters.append(query.casefold())
        selection = " FROM entries e JOIN profiles p ON p.id=e.profile WHERE " + " AND ".join(
            conditions
        )
        with self.connect() as db:
            if query:
                db.create_function("casefold", 1, str.casefold, deterministic=True)
            # Count and page must describe the same snapshot even during migration.
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*)" + selection, parameters).fetchone()[0]
            offset = (max(1, page) - 1) * page_size
            if offset >= total:
                return total, []
            rows = db.execute(
                "SELECT e.*" + selection + " ORDER BY e.subject_id LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()
        return total, self._decode_rows(rows)

    def report_rows(self, pid):
        # Reports need presence flags, not target metadata, plans or candidate lists.
        # Keep the row shape so the migration/report rules remain in Migrator.
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.status, e.migrated_fields,
                json_object(
                    'type', json_extract(e.source, '$.type'),
                    'rate', json_extract(e.source, '$.rate'),
                    'comment', json_extract(e.source, '$.comment'),
                    'tags', json_extract(e.source, '$.tags'),
                    'ep_status', json_extract(e.source, '$.ep_status'),
                    'vol_status', json_extract(e.source, '$.vol_status')) AS source,
                CASE WHEN e.item IS NULL OR json(e.item) IN ('null', '{}')
                    THEN 'false' ELSE 'true' END AS item,
                CASE WHEN e.plan IS NULL OR json(e.plan) IN ('null', '{}')
                    THEN 'false' ELSE 'true' END AS plan,
                CASE WHEN e.resolution IS NULL OR json(e.resolution) IN ('null', '{}')
                    THEN NULL ELSE json_object(
                        'retryable', json_extract(e.resolution, '$.retryable')) END AS resolution
                FROM entries e JOIN profiles p ON p.id=e.profile
                WHERE e.profile=? AND e.scan=p.scan""",
                (pid,),
            ).fetchall()
        return self._decode_rows(rows)

    @staticmethod
    def _decode_rows(rows):
        result = []
        for raw in rows:
            row = dict(raw)
            for key in (
                "source",
                "item",
                "plan",
                "migrated_fields",
                "resolution",
                "subject_detail",
            ):
                if key in row:
                    row[key] = json.loads(row[key]) if row[key] else None
            result.append(row)
        return result

    def has_migrated_target(self, pid, sid, item_uuid):
        with self.connect() as db:
            return (
                db.execute(
                    """SELECT 1 FROM entries e JOIN profiles p ON p.id=e.profile
                WHERE e.profile=? AND e.scan=p.scan AND e.subject_id<>?
                AND e.status='migrated' AND json_extract(e.item, '$.uuid')=? LIMIT 1""",
                    (pid, sid, item_uuid),
                ).fetchone()
                is not None
            )

    def update(self, pid, sid, **fields):
        allowed = {
            "item",
            "plan",
            "status",
            "stage",
            "error",
            "attempts",
            "migrated_at",
            "migrated_fields",
            "resolution",
            "subject_detail",
        }
        if not fields.keys() <= allowed:
            raise ValueError("Unknown database field")
        values = [
            encode(v)
            if k in ("item", "plan", "migrated_fields", "resolution", "subject_detail")
            and v is not None
            else v
            for k, v in fields.items()
        ]
        assignments = ",".join(f"{k}=?" for k in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE entries SET {assignments},updated_at=? WHERE profile=? AND subject_id=?",
                (*values, now(), pid, sid),
            )
