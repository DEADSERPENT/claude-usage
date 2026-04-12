"""
invoice.py - Static HTML report and invoice generator for claude-usage.

Generates standalone, portable HTML files with inline CSS/JS and embedded
JSON data. Can be emailed as formal invoices or expense reports.
"""

import json
import sqlite3
from datetime import date, timedelta, datetime
from pathlib import Path

from config import DB_PATH, calc_cost, PRICING


def generate_invoice(db_path: Path = DB_PATH, project: str = None,
                     date_from: str = None, date_to: str = None,
                     client_name: str = None, output_path: Path = None,
                     tag: str = None) -> dict:
    """Generate a standalone HTML invoice/report."""
    if not db_path.exists():
        return {"status": "error", "message": "Database not found"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT s.session_id, s.project_name, s.first_timestamp, s.last_timestamp,
               s.git_branch, s.model, s.turn_count,
               s.total_input_tokens, s.total_output_tokens,
               s.total_cache_read, s.total_cache_creation
        FROM sessions s
        WHERE 1=1
    """
    params = []
    if project:
        query += " AND s.project_name LIKE ?"
        params.append(f"%{project}%")
    if date_from:
        query += " AND s.last_timestamp >= ?"
        params.append(date_from)
    if date_to:
        query += " AND s.last_timestamp <= ?"
        params.append(date_to + "T23:59:59")
    if tag:
        query += """ AND s.session_id IN (
            SELECT session_id FROM tags WHERE tag_name = ?
        )"""
        params.append(tag)
    query += " ORDER BY s.last_timestamp DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return {"status": "error", "message": "No sessions match the criteria"}

    sessions = []
    total_cost = 0
    total_input = total_output = total_cache_read = total_cache_creation = 0
    total_turns = 0
    models_used = set()

    for r in rows:
        cost = calc_cost(r["model"], r["total_input_tokens"] or 0,
                        r["total_output_tokens"] or 0,
                        r["total_cache_read"] or 0,
                        r["total_cache_creation"] or 0)
        total_cost += cost
        total_input += r["total_input_tokens"] or 0
        total_output += r["total_output_tokens"] or 0
        total_cache_read += r["total_cache_read"] or 0
        total_cache_creation += r["total_cache_creation"] or 0
        total_turns += r["turn_count"] or 0
        models_used.add(r["model"] or "unknown")
        sessions.append({
            "id": r["session_id"][:8],
            "project": r["project_name"] or "unknown",
            "branch": r["git_branch"] or "",
            "model": r["model"] or "unknown",
            "date": (r["last_timestamp"] or "")[:10],
            "turns": r["turn_count"] or 0,
            "input": r["total_input_tokens"] or 0,
            "output": r["total_output_tokens"] or 0,
            "cost": round(cost, 6),
        })

    report_data = {
        "generated_at": datetime.now().isoformat(),
        "client": client_name or "N/A",
        "project_filter": project or "All Projects",
        "date_range": f"{date_from or 'start'} to {date_to or 'now'}",
        "total_cost": round(total_cost, 4),
        "total_input": total_input,
        "total_output": total_output,
        "total_cache_read": total_cache_read,
        "total_cache_creation": total_cache_creation,
        "total_turns": total_turns,
        "total_sessions": len(sessions),
        "models_used": sorted(models_used),
        "sessions": sessions,
    }

    html = _build_invoice_html(report_data)

    if output_path is None:
        safe_name = (project or "all").replace("/", "_").replace(" ", "_")[:30]
        output_path = Path(f"invoice_{safe_name}_{date.today().isoformat()}.html")

    output_path.write_text(html, encoding="utf-8")

    return {
        "status": "created",
        "path": str(output_path),
        "total_cost": report_data["total_cost"],
        "sessions": len(sessions),
        "size_bytes": output_path.stat().st_size,
    }


def _fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return f"{n:,}"


def _build_invoice_html(data: dict) -> str:
    sessions_json = json.dumps(data["sessions"], indent=2)
    rows_html = ""
    for s in data["sessions"]:
        rows_html += f"""<tr>
            <td>{s['id']}</td><td>{s['project']}</td><td>{s['branch']}</td>
            <td>{s['model']}</td><td>{s['date']}</td><td>{s['turns']}</td>
            <td>{_fmt(s['input'])}</td><td>{_fmt(s['output'])}</td>
            <td class="cost">${s['cost']:.4f}</td>
        </tr>\n"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; color: #1a1a1a; max-width: 900px; margin: 0 auto; padding: 40px 24px; }}
  .header {{ border-bottom: 3px solid #E07A5F; padding-bottom: 20px; margin-bottom: 30px; }}
  .header h1 {{ font-size: 24px; color: #1a1a1a; margin-bottom: 4px; }}
  .header p {{ color: #6B7280; font-size: 13px; }}
  .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 30px; }}
  .meta-card {{ background: #f9f9f9; border: 1px solid #e5e5e5; border-radius: 8px; padding: 14px; }}
  .meta-card .label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; color: #6B7280; margin-bottom: 4px; }}
  .meta-card .value {{ font-size: 20px; font-weight: 700; }}
  .cost-value {{ color: #2E9E5A; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 30px; }}
  th {{ text-align: left; padding: 8px 10px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; color: #6B7280; border-bottom: 2px solid #e5e5e5; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }}
  td.cost {{ font-family: monospace; color: #2E9E5A; font-weight: 600; }}
  tr:hover td {{ background: #fafafa; }}
  .footer {{ border-top: 1px solid #e5e5e5; padding-top: 16px; color: #9CA3AF; font-size: 11px; }}
  .total-row td {{ font-weight: 700; border-top: 2px solid #1a1a1a; background: #f9f9f9; }}
  @media print {{
    body {{ padding: 20px; }}
    .no-print {{ display: none; }}
  }}
  .btn {{ display: inline-block; padding: 8px 16px; background: #E07A5F; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; text-decoration: none; margin-right: 8px; }}
  .btn:hover {{ opacity: 0.9; }}
  .btn-outline {{ background: transparent; color: #E07A5F; border: 1px solid #E07A5F; }}
</style>
</head>
<body>
<div class="header">
  <h1>Claude Code Usage Report</h1>
  <p>Generated: {data['generated_at'][:16]} &middot; Client: {data['client']} &middot; {data['date_range']}</p>
</div>

<div class="meta-grid">
  <div class="meta-card"><div class="label">Total Cost</div><div class="value cost-value">${data['total_cost']:.4f}</div></div>
  <div class="meta-card"><div class="label">Sessions</div><div class="value">{data['total_sessions']}</div></div>
  <div class="meta-card"><div class="label">Turns</div><div class="value">{_fmt(data['total_turns'])}</div></div>
  <div class="meta-card"><div class="label">Input Tokens</div><div class="value">{_fmt(data['total_input'])}</div></div>
  <div class="meta-card"><div class="label">Output Tokens</div><div class="value">{_fmt(data['total_output'])}</div></div>
  <div class="meta-card"><div class="label">Models Used</div><div class="value" style="font-size:13px">{', '.join(data['models_used'])}</div></div>
</div>

<div class="no-print" style="margin-bottom:20px">
  <button class="btn" onclick="window.print()">Print / Save PDF</button>
  <button class="btn btn-outline" onclick="downloadCSV()">Download CSV</button>
</div>

<table>
<thead><tr>
  <th>Session</th><th>Project</th><th>Branch</th><th>Model</th>
  <th>Date</th><th>Turns</th><th>Input</th><th>Output</th><th>Cost</th>
</tr></thead>
<tbody>
{rows_html}
<tr class="total-row">
  <td colspan="5"><strong>TOTAL</strong></td>
  <td>{_fmt(data['total_turns'])}</td>
  <td>{_fmt(data['total_input'])}</td>
  <td>{_fmt(data['total_output'])}</td>
  <td class="cost">${data['total_cost']:.4f}</td>
</tr>
</tbody>
</table>

<div class="footer">
  <p>This report was generated by Claude Code Usage Dashboard. Cost estimates are based on Anthropic API pricing.</p>
  <p>Project filter: {data['project_filter']} &middot; Date range: {data['date_range']}</p>
</div>

<script>
const reportData = {sessions_json};
function downloadCSV() {{
  const headers = ['Session','Project','Branch','Model','Date','Turns','Input','Output','Cost'];
  const rows = reportData.map(s => [s.id,s.project,s.branch,s.model,s.date,s.turns,s.input,s.output,s.cost]);
  const csv = [headers.join(','), ...rows.map(r => r.map(v => '"'+String(v).replace(/"/g,'""')+'"').join(','))].join('\\n');
  const blob = new Blob([csv], {{type:'text/csv'}});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'usage_report.csv'; a.click();
}}
</script>
</body>
</html>"""
