"""Render an owner audit into a local timing report; no provider calls.

Gaps are caller/transport time, not measured model inference or vision latency.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timedelta
import html
import json
from pathlib import Path

PUBLIC = {'capabilities', 'status', 'session_start', 'session_stop', 'observe', 'candidates', 'act', 'cancel', 'stop_all'}

def timeline(audit: Path, output: Path):
    records = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    rows = []
    for r in records:
        if r.get('tool') not in PUBLIC or r.get('status') == 'pending' or r.get('transport') != 'local-mcp':
            continue
        end = datetime.fromisoformat(r['timestamp'])
        duration = r.get('duration_ms')
        start = end - timedelta(milliseconds=duration) if isinstance(duration, (int, float)) else end
        rows.append((start, end, r, duration))
    rows.sort(key=lambda row: row[0])
    if not rows:
        raise ValueError('No completed MCP calls in audit')
    zero = rows[0][0]
    total = (max(r[1] for r in rows) - zero).total_seconds()
    calls = []
    previous = zero
    for start, end, record, duration in rows:
        calls.append({'tool': record['tool'], 'app': record.get('app', ''), 'status': record['status'],
                      'start_s': round((start-zero).total_seconds(), 3),
                      'end_s': round((end-zero).total_seconds(), 3), 'duration_ms': duration,
                      'gap_before_s': round(max(0, (start-previous).total_seconds()), 3)})
        previous = max(previous, end)
    summary = {'elapsed_s': round(total, 3), 'call_count': len(calls),
               'calls_by_tool': dict(Counter(c['tool'] for c in calls)),
               'tool_seconds': round(sum(c['duration_ms'] or 0 for c in calls)/1000, 3),
               'duration_available_for_all_calls': all(c['duration_ms'] is not None for c in calls),
               'calls': calls,
               'note': 'Gaps include caller reasoning, image interpretation and transport; those are not isolated.'}
    output.mkdir(parents=True, exist_ok=True)
    (output/'timeline.json').write_text(json.dumps(summary, indent=2)+'\n')
    lines = ['# Workflow timeline', '', f"Elapsed: **{total:.1f} s** · completed MCP calls: **{len(calls)}**", '',
             'Times include owner work. Gaps include caller reasoning and transport; they do not isolate vision latency.', '',
             '| Step | App | Start (s) | Tool (s) | Gap before (s) | Result |', '|---|---|---:|---:|---:|---|']
    bars = []
    for i,c in enumerate(calls):
        seconds = 'unavailable' if c['duration_ms'] is None else f"{c['duration_ms']/1000:.2f}"
        lines.append(f"| {c['tool']} | {c['app']} | {c['start_s']:.2f} | {seconds} | {c['gap_before_s']:.2f} | {c['status']} |")
        left = c['start_s']/max(total, .001)*100
        width = max(.15, (c['end_s']-c['start_s'])/max(total,.001)*100)
        label = html.escape(f"{i+1}. {c['tool']} {c['app']}")
        detail = html.escape(f"Start {c['start_s']}s; duration {seconds}s; preceding gap {c['gap_before_s']}s; {c['status']}")
        bars.append(f'<div class="row"><span>{label}</span><div class="track"><div class="bar" style="left:{left}%;width:{width}%" title="{detail}"></div></div><small>{detail}</small></div>')
    (output/'timeline.md').write_text('\n'.join(lines)+'\n')
    (output/'timeline.html').write_text('''<!doctype html><meta charset="utf-8"><title>Computer-use workflow timing</title>
<style>body{font:15px system-ui;margin:32px;background:#111923;color:#e9edf3;max-width:1400px}h1{font-size:26px}.row{display:grid;grid-template-columns:230px 1fr;gap:10px;margin:14px 0}.track{position:relative;background:#253243;height:20px;border-radius:4px}.bar{position:absolute;background:#58c4bd;height:20px;border-radius:4px;min-width:2px}small{grid-column:2;color:#aebbc9}p{max-width:850px;color:#b6c5d4}</style>
''' + f'<h1>Workflow timeline · {total:.1f} seconds</h1><p>Teal bars are measured owner tool work. Empty gaps include agent reasoning, image interpretation and transport. Hover for details. Timing does not isolate vision inference.</p>' + ''.join(bars))
    return summary

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('audit', type=Path); p.add_argument('--output', required=True, type=Path)
    a=p.parse_args(); result=timeline(a.audit,a.output)
    print(json.dumps({k:v for k,v in result.items() if k != 'calls'},indent=2))
