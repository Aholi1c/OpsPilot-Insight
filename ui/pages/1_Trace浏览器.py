# -*- coding: utf-8 -*-
"""Trace Browser — Span tree with duration bars + audit events table."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_loader import list_runs, load_json, load_jsonl  # noqa: E402
from theme import (  # noqa: E402
    Colors, inject_css, render_header, section_title,
    stat_cell, html_table, status_dot, empty_state,
)

st.set_page_config(page_title="Traces | OpsPilot Insight", layout="wide", initial_sidebar_state="collapsed")
inject_css()
render_header()

# ─── Data ────────────────────────────────────────────────────────────────────
runs = list_runs()
if not runs:
    empty_state("No run artifacts found. Execute replay_eval.py first.")
    st.stop()

run = st.selectbox("Select run", runs, format_func=lambda r: r["label"], label_visibility="collapsed")
report = run["report"]

# ─── Info bar ────────────────────────────────────────────────────────────────
trace = load_json(run["trace_path"])
span_count = trace.get("span_count", len(trace.get("spans", []))) if trace else 0
total_dur = trace.get("total_duration_ms", 0) if trace else 0
if not total_dur and trace:
    spans = trace.get("spans", [])
    if spans:
        total_dur = sum(s.get("duration_ms", 0) for s in spans if not s.get("parent_span_id"))

st.markdown(f"""
<div class="info-bar">
    <div>Scenario: <span class="ib-val">{run["scenario"]}</span></div>
    <div>Trace: <span class="ib-val">{run["trace_id"][:12]}</span></div>
    <div>Duration: <span class="ib-val">{total_dur:.1f} ms</span></div>
    <div>Spans: <span class="ib-val">{span_count}</span></div>
</div>
""", unsafe_allow_html=True)

# ─── Span tree ───────────────────────────────────────────────────────────────
section_title("TRACE OVERVIEW")

if not trace:
    empty_state(f"Trace file missing: {run['trace_path'].name}")
else:
    spans = trace.get("spans", [])
    children: dict = {}
    by_id: dict = {}
    max_dur = 0.01

    for s in spans:
        by_id[s["span_id"]] = s
        children.setdefault(s.get("parent_span_id"), []).append(s)
        dur = s.get("duration_ms", 0)
        if isinstance(dur, (int, float)) and dur > max_dur:
            max_dur = dur

    for bucket in children.values():
        bucket.sort(key=lambda s: s.get("start_time") or 0)

    def _dur_color(ms: float) -> str:
        if ms < 10:
            return Colors.BLUE
        if ms < 100:
            return Colors.GREEN
        if ms < 1000:
            return Colors.YELLOW
        return Colors.RED

    def _build(span, depth, lines):
        dur = span.get("duration_ms", 0)
        dur_txt = f"{dur:.2f} ms" if isinstance(dur, (int, float)) else "—"
        name = span.get("name", "")

        # Tree connector lines
        if depth == 0:
            connector = ""
        else:
            connector = "│   " * (depth - 1) + "├── "

        bar_w = max(3, int((dur / max_dur) * 140)) if isinstance(dur, (int, float)) else 3
        bar_color = _dur_color(dur if isinstance(dur, (int, float)) else 0)

        lines.append(f"""<div class="span-row">
<span class="tree-line">{connector}</span>
<span class="span-name">{name}</span>
<span class="dur-bar" style="background:{bar_color};width:{bar_w}px;"></span>
<span class="dur-val">{dur_txt}</span>
</div>""")

        for child in children.get(span["span_id"], []):
            _build(child, depth + 1, lines)

    tree_lines = []
    for root in children.get(None, []):
        _build(root, 0, tree_lines)

    tree_html = "\n".join(tree_lines)
    st.markdown(f'<div style="padding:8px 0;font-family:JetBrains Mono,monospace;">{tree_html}</div>', unsafe_allow_html=True)

# ─── Audit events table ─────────────────────────────────────────────────────
st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
section_title("AUDIT EVENTS")

audit_events = load_jsonl(run["audit_path"])
if not audit_events:
    empty_state(f"No audit file: {run['audit_path'].name}")
else:
    rows = []
    for ev in audit_events:
        ts = ev.get("timestamp", "")
        time_display = ts.split("T")[1][:8] if "T" in ts else ts[-8:]
        event_type = ev.get("event_type", "")

        # Dot color by type; execute events colored by status field
        type_colors = {
            "whitelist_check": Colors.BLUE,
            "approval": Colors.GREEN,
            "checkpoint": Colors.TEXT_SECONDARY,
            "execute": Colors.GREEN,
            "rollback": Colors.YELLOW,
            "budget_alert": Colors.RED,
        }
        dot_color = type_colors.get(event_type, Colors.TEXT_SECONDARY)
        if event_type == "execute" and ev.get("status") == "failed":
            dot_color = Colors.RED

        extra = {k: v for k, v in ev.items()
                 if k not in ("timestamp", "event_type", "trace_id", "span_id")}
        detail = ", ".join(f"{k}={v}" for k, v in extra.items())[:100] if extra else ""

        status_html = status_dot(dot_color, event_type)
        rows.append([time_display, status_html, f'<span style="color:{Colors.TEXT_SECONDARY};font-size:11px;">{detail}</span>'])

    tbl = html_table(["Time", "Event", "Details"], rows)
    st.markdown(tbl, unsafe_allow_html=True)
