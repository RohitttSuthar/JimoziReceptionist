from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "query_analytics.sqlite3"
_DB_PATH = Path(os.getenv("ANALYTICS_DB_PATH", str(_DEFAULT_DB)))
_LOCK = threading.Lock()


def _connect():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_analytics():
    with _LOCK, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                product_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                query_text TEXT NOT NULL,
                normalized_query TEXT NOT NULL,
                success INTEGER NOT NULL,
                failure_reason TEXT,
                answer_mode TEXT,
                match_score REAL,
                source_count INTEGER NOT NULL DEFAULT 0,
                latency_ms REAL NOT NULL DEFAULT 0,
                session_hash TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_events_created ON query_events(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_events_product ON query_events(product_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_events_failure ON query_events(success, created_at)")
        conn.commit()


def record_query(*, product_id, mode, query_text, success, failure_reason=None,
                 answer_mode=None, match_score=None, source_count=0, latency_ms=0,
                 session_hash=None):
    import re
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", query_text.lower())).strip()
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect() as conn:
        conn.execute("""
            INSERT INTO query_events
            (created_at, product_id, mode, query_text, normalized_query, success,
             failure_reason, answer_mode, match_score, source_count, latency_ms, session_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, product_id, mode, query_text[:4000], normalized[:1000],
              int(bool(success)), failure_reason, answer_mode, match_score,
              int(source_count or 0), float(latency_ms or 0), session_hash))
        conn.commit()


def get_analytics(days=7, product_id=None):
    days = max(1, min(int(days or 7), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where = "WHERE created_at >= ?"
    params = [since]
    if product_id:
        where += " AND product_id = ?"
        params.append(product_id)

    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM query_events {where} ORDER BY created_at DESC", params).fetchall()
        total = len(rows)
        failed = [r for r in rows if not r["success"]]
        successful = total - len(failed)
        avg_latency = round(sum(r["latency_ms"] for r in rows) / total, 1) if total else 0
        daily = {}
        modes = {}
        reasons = {}
        repeated = {}
        hourly = {str(h).zfill(2): 0 for h in range(24)}
        for r in rows:
            day = r["created_at"][:10]
            daily.setdefault(day, {"date": day, "total": 0, "failed": 0, "successful": 0})
            daily[day]["total"] += 1
            daily[day]["failed" if not r["success"] else "successful"] += 1
            modes.setdefault(r["mode"], {"mode": r["mode"], "total": 0, "failed": 0})
            modes[r["mode"]]["total"] += 1
            modes[r["mode"]]["failed"] += int(not r["success"])
            if not r["success"]:
                reason = r["failure_reason"] or r["answer_mode"] or "unknown"
                reasons[reason] = reasons.get(reason, 0) + 1
                key = r["normalized_query"] or "(empty)"
                item = repeated.setdefault(key, {"query": r["query_text"], "count": 0, "last_seen": r["created_at"], "reasons": {}})
                item["count"] += 1
                item["last_seen"] = max(item["last_seen"], r["created_at"])
                item["reasons"][reason] = item["reasons"].get(reason, 0) + 1
            try:
                hour = r["created_at"][11:13]
                if hour in hourly:
                    hourly[hour] += 1
            except Exception:
                pass

        top_failing = sorted(repeated.values(), key=lambda x: (-x["count"], x["query"]))[:20]
        for item in top_failing:
            item["reasons"] = sorted(
                [{"reason": k, "count": v} for k, v in item["reasons"].items()],
                key=lambda x: -x["count"]
            )
        recent = [{
            "time": r["created_at"], "product_id": r["product_id"], "mode": r["mode"],
            "query": r["query_text"], "success": bool(r["success"]),
            "failure_reason": r["failure_reason"], "answer_mode": r["answer_mode"],
            "match_score": r["match_score"], "source_count": r["source_count"],
            "latency_ms": r["latency_ms"]
        } for r in rows[:100]]
        products = conn.execute(
            f"SELECT product_id, COUNT(*) AS total, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed "
            f"FROM query_events {where} GROUP BY product_id ORDER BY total DESC", params
        ).fetchall()

    return {
        "period_days": days,
        "summary": {
            "total_queries": total, "successful_queries": successful, "failed_queries": len(failed),
            "failure_rate": round((len(failed) / total) * 100, 2) if total else 0,
            "success_rate": round((successful / total) * 100, 2) if total else 0,
            "avg_latency_ms": avg_latency,
            "avg_failed_match_score": round(sum((r["match_score"] or 0) for r in failed) / len(failed), 3) if failed else 0
        },
        "daily": sorted(daily.values(), key=lambda x: x["date"]),
        "modes": sorted(modes.values(), key=lambda x: x["mode"]),
        "failure_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
        "top_failing_queries": top_failing,
        "hourly": [{"hour": k, "count": v} for k, v in hourly.items()],
        "products": [dict(r) for r in products],
        "recent_queries": recent
    }
