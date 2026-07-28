# -*- coding: utf-8 -*-
"""Evaluation Report — Score breakdown, dimension table, judge comments."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_loader import get_golden_dataset, latest_eval_report  # noqa: E402
from theme import (  # noqa: E402
    Colors, PLOTLY_LAYOUT, inject_css, render_header, section_title,
    stat_cell, html_table, empty_state, format_score,
)

st.set_page_config(page_title="Evaluation | OpsPilot Insight", layout="wide", initial_sidebar_state="collapsed")
inject_css()
render_header()

# ─── Data ────────────────────────────────────────────────────────────────────
report = latest_eval_report()
golden = get_golden_dataset()

if not report:
    empty_state("No evaluation report found. Run: python scripts/replay_eval.py")
    st.stop()

results = report.get("results", [])

# ─── Stat row ────────────────────────────────────────────────────────────────
cols = st.columns(4)
with cols[0]:
    score = report.get("overall_score", 0)
    stat_cell(f"{format_score(score)}/100", "Overall Score")
with cols[1]:
    stat_cell(report.get("sample_count", 0), "Samples")
with cols[2]:
    passed = report.get("all_passed_85", False)
    rate = "100%" if passed else f"{sum(1 for r in results if r.get('total_score', 0) >= 85)}/{len(results)}"
    stat_cell(rate, "Pass Rate (≥85)")
with cols[3]:
    gen_time = report.get("generated_at", "")[:16]
    stat_cell(gen_time, "Report Time")

st.markdown(f'<div style="border-bottom:1px solid {Colors.BORDER};margin:8px 0 20px 0;"></div>', unsafe_allow_html=True)

# ─── Main: bar chart + dimension table ──────────────────────────────────────
left_col, right_col = st.columns([6, 4])

dimensions = ["root_cause", "action_type", "verification", "loop_completeness", "safety_compliance"]
dim_labels = ["Root Cause", "Action Match", "Verification", "Loop Complete", "Safety"]

with left_col:
    section_title("SCENARIO SCORES")
    if results:
        case_ids = [r["case_id"].replace("GOLDEN-", "") for r in results]
        scores = [r["total_score"] for r in results]

        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=case_ids,
            x=scores,
            orientation="h",
            marker_color=Colors.BLUE,
            text=[format_score(s) for s in scores],
            textposition="outside",
            textfont=dict(color=Colors.TEXT_PRIMARY, size=11),
        ))
        fig.add_vline(x=85, line_dash="dash", line_color=Colors.TEXT_SECONDARY, line_width=1,
                      annotation_text="85", annotation_font_color=Colors.TEXT_SECONDARY,
                      annotation_font_size=9)
        fig.update_layout(
            **PLOTLY_LAYOUT,
            height=max(180, len(results) * 60),
            showlegend=False,
        )
        fig.update_xaxes(range=[0, 110], gridcolor=Colors.BORDER)
        fig.update_yaxes(gridcolor=Colors.BORDER)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

with right_col:
    section_title("DIMENSION BREAKDOWN")
    if results:
        # Aggregate average per dimension
        dim_avgs = {}
        for dim_key, dim_label in zip(dimensions, dim_labels):
            vals = []
            for r in results:
                rules = r.get("rules", {})
                v = rules.get(dim_key, {}).get("score")
                if isinstance(v, (int, float)):
                    vals.append(v)
            dim_avgs[dim_label] = sum(vals) / len(vals) if vals else 0

        rows = []
        for dim_label in dim_labels:
            v = dim_avgs[dim_label]
            rows.append([
                dim_label,
                f'<span style="color:{Colors.TEXT_WHITE}">{format_score(v)}</span>',
                "100",
            ])
        tbl = html_table(["Dimension", "Score", "Max"], rows,
                         col_styles=["", "text-align:right", "text-align:right;color:#8b8b8b"])
        st.markdown(tbl, unsafe_allow_html=True)

# ─── Judge comments ──────────────────────────────────────────────────────────
st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
section_title("JUDGE COMMENTS")

for r in results:
    judge = r.get("judge") or {}
    scores = judge.get("scores", [])
    if not scores:
        continue

    case_name = r["case_id"].replace("GOLDEN-", "")
    with st.expander(f"{case_name} — {format_score(r['total_score'])}"):
        for s in scores:
            score_val = s.get("score", 0)
            max_score = 5
            pct = score_val / max_score * 100
            bar_color = Colors.GREEN if score_val >= 4 else (Colors.YELLOW if score_val >= 3 else Colors.RED)

            st.markdown(f"""
            <div style="margin-bottom:10px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
                    <span style="font-size:12px;color:{Colors.TEXT_PRIMARY};">{s.get('label', '')}</span>
                    <span style="font-size:12px;color:{Colors.TEXT_SECONDARY};font-family:JetBrains Mono,monospace;">{score_val}/{max_score}</span>
                </div>
                <div class="pbar"><div class="pbar-fill" style="width:{pct}%;background:{bar_color};"></div></div>
                <div style="font-size:11px;color:{Colors.TEXT_SECONDARY};margin-top:3px;">{s.get('comment', '')}</div>
            </div>
            """, unsafe_allow_html=True)
