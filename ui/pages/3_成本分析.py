# -*- coding: utf-8 -*-
"""Cost Analysis — per-Agent/Skill/Model breakdown, trend, LLM call details."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_loader import PROJECT_ROOT, list_runs, load_json, get_cost_trend  # noqa: E402
from opspilot.evaluation.cost import load_pricing, estimate_call_cost  # noqa: E402  (src/ 已由 data_loader 注入 sys.path)
from theme import (  # noqa: E402
    Colors, PLOTLY_LAYOUT, inject_css, render_header, section_title,
    stat_cell, status_dot, html_table, empty_state, format_cost,
)

st.set_page_config(page_title="Cost | OpsPilot Insight", layout="wide", initial_sidebar_state="collapsed")
inject_css()
render_header()

# ─── Data ────────────────────────────────────────────────────────────────────
runs = [r for r in list_runs() if r["metrics_path"].exists()]
if not runs:
    empty_state("No run artifacts with metrics. Execute replay_eval.py first.")
    st.stop()

run = st.selectbox("Select run", runs, format_func=lambda r: r["label"], label_visibility="collapsed")
metrics = load_json(run["metrics_path"]) or {}
cost = metrics.get("cost") or {}

if not cost:
    empty_state(f"No cost data in: {run['metrics_path'].name}")
    st.stop()

budget = cost.get("budget") or {}

# ─── Stat row ────────────────────────────────────────────────────────────────
total_cost = cost.get("total_cost", 0)
limit = budget.get("limit", 0)
ratio = budget.get("usage_ratio")
exceeded = budget.get("exceeded", False)

cols = st.columns(4)
with cols[0]:
    stat_cell(format_cost(total_cost), "Run Cost")
with cols[1]:
    stat_cell(f"¥{limit:.2f}" if limit else "—", "Budget Limit")
with cols[2]:
    ratio_str = f"{ratio * 100:.1f}%" if isinstance(ratio, (int, float)) else "—"
    stat_cell(ratio_str, "Usage Ratio")
    # Thin progress bar
    if isinstance(ratio, (int, float)):
        bar_color = Colors.GREEN if ratio < 0.8 else (Colors.YELLOW if ratio < 1.0 else Colors.RED)
        pct = min(ratio * 100, 100)
        st.markdown(f'<div class="pbar"><div class="pbar-fill" style="width:{pct:.0f}%;background:{bar_color};"></div></div>', unsafe_allow_html=True)
with cols[3]:
    if exceeded:
        st.markdown(f'<div class="stat-cell"><div class="stat-value" style="font-size:22px;">{status_dot(Colors.RED, "EXCEEDED")}</div><div class="stat-label">Budget Status</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="stat-cell"><div class="stat-value" style="font-size:22px;">{status_dot(Colors.GREEN, "WITHIN")}</div><div class="stat-label">Budget Status</div></div>', unsafe_allow_html=True)

st.markdown(f'<div style="border-bottom:1px solid {Colors.BORDER};margin:12px 0 20px 0;"></div>', unsafe_allow_html=True)

# ─── Three-column breakdown ──────────────────────────────────────────────────
section_title("COST BREAKDOWN")
c1, c2, c3 = st.columns(3)


def _bar_chart(data: dict, title: str, color_idx: int = 0):
    """Horizontal bar chart for cost breakdown."""
    if not data:
        empty_state(f"No {title} data")
        return
    names = list(data.keys())
    values = list(data.values())
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=names, x=values, orientation="h",
        marker_color=Colors.CHART_PALETTE[color_idx % len(Colors.CHART_PALETTE)],
        text=[f"¥{v:.4f}" for v in values],
        textposition="outside",
        textfont=dict(color=Colors.TEXT_SECONDARY, size=10),
    ))
    fig.update_layout(**PLOTLY_LAYOUT, height=max(140, len(names) * 40), showlegend=False)
    fig.update_xaxes(gridcolor=Colors.BORDER, showticklabels=False)
    fig.update_yaxes(gridcolor=Colors.BORDER)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


with c1:
    st.markdown(f'<div style="font-size:11px;color:{Colors.TEXT_SECONDARY};text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">Per Agent</div>', unsafe_allow_html=True)
    _bar_chart(cost.get("per_agent", {}), "per-agent", 0)

with c2:
    st.markdown(f'<div style="font-size:11px;color:{Colors.TEXT_SECONDARY};text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">Per Skill</div>', unsafe_allow_html=True)
    _bar_chart(cost.get("per_skill", {}), "per-skill", 0)

with c3:
    st.markdown(f'<div style="font-size:11px;color:{Colors.TEXT_SECONDARY};text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">Per Model</div>', unsafe_allow_html=True)
    _bar_chart(cost.get("per_model", {}), "per-model", 0)

# ─── Cross-run trend ─────────────────────────────────────────────────────────
st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
section_title("CROSS-RUN TREND")

trend = get_cost_trend()
trend = [t for t in trend if t.get("total_cost")]

if not trend:
    empty_state("No historical cost data")
else:
    fig = go.Figure()
    x_labels = [t.get("generated_at", "")[:16] for t in trend]
    costs = [t["total_cost"] for t in trend]

    fig.add_trace(go.Scatter(
        x=x_labels, y=costs,
        mode="lines",
        fill="tozeroy",
        fillcolor="rgba(52,152,219,0.08)",
        line=dict(color=Colors.BLUE, width=1.5),
        hovertemplate="%{x}<br>¥%{y:.4f}<extra></extra>",
    ))

    if budget.get("limit"):
        fig.add_hline(
            y=budget["limit"], line_dash="dot", line_color=Colors.RED, line_width=1,
            annotation_text=f"budget ¥{budget['limit']:.2f}",
            annotation_font_color=Colors.RED, annotation_font_size=9,
        )

    fig.update_layout(**PLOTLY_LAYOUT, height=240, showlegend=False)
    fig.update_xaxes(gridcolor=Colors.BORDER, tickangle=-30, tickfont=dict(size=9))
    fig.update_yaxes(gridcolor=Colors.BORDER, title="CNY")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# ─── LLM call detail table ───────────────────────────────────────────────────
st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
section_title("LLM CALL DETAILS")

calls = (metrics.get("llm") or {}).get("calls", [])
if calls:
    pricing = load_pricing(PROJECT_ROOT / "config" / "pricing.yaml")  # 与 metrics.cost 同口径
    rows = []
    for c in calls:
        total_tokens = c.get("prompt_tokens", 0) + c.get("completion_tokens", 0)
        est_cost = estimate_call_cost(c, pricing)
        rows.append([
            c.get("agent", "—"),
            c.get("skill", "") or "—",
            c.get("model", "—"),
            str(total_tokens),
            format_cost(est_cost),
        ])
    tbl = html_table(
        ["Agent", "Skill", "Model", "Tokens", "Est. Cost"],
        rows,
        col_styles=["", "", "", "text-align:right", "text-align:right"],
    )
    st.markdown(tbl, unsafe_allow_html=True)
else:
    empty_state("No LLM call records")
