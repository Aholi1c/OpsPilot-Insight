# -*- coding: utf-8 -*-
"""OpsPilot Insight — Overview dashboard.

Startup: streamlit run ui/dashboard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import OUTPUT_DIR, get_golden_dataset, latest_eval_report, list_runs, load_json  # noqa: E402
from theme import (  # noqa: E402
    Colors, PLOTLY_LAYOUT, inject_css, render_header, section_title,
    stat_cell, status_dot, html_table, empty_state, format_score, format_cost,
)

st.set_page_config(page_title="OpsPilot Insight", layout="wide", initial_sidebar_state="collapsed")
inject_css()

# ─── Header ──────────────────────────────────────────────────────────────────
render_header()

# ─── Data ────────────────────────────────────────────────────────────────────
runs = list_runs()
eval_report = latest_eval_report()
golden = get_golden_dataset()

budget_flags = 0
for r in runs:
    if r["metrics_path"].exists():
        m = load_json(r["metrics_path"])
        if m and (m.get("cost") or {}).get("budget", {}).get("exceeded"):
            budget_flags += 1

# ─── Stat cells row ──────────────────────────────────────────────────────────
cols = st.columns(4)
with cols[0]:
    stat_cell(len(runs), "Runs")
with cols[1]:
    stat_cell(len(golden), "Golden Samples")
with cols[2]:
    score = eval_report.get("overall_score", "—") if eval_report else "—"
    stat_cell(format_score(score) if isinstance(score, (int, float)) else score, "Avg Score")
with cols[3]:
    stat_cell(budget_flags, "Budget Alerts")

st.markdown(f'<div style="border-bottom:1px solid {Colors.BORDER};margin:8px 0 20px 0;"></div>', unsafe_allow_html=True)

# ─── Main area ───────────────────────────────────────────────────────────────
if not runs:
    empty_state("No run artifacts found. Execute: python run_demo.py --scenario db_pool_exhaustion --auto-approve")
else:
    left_col, right_col = st.columns([6, 4])

    with left_col:
        section_title("RUN HISTORY")
        # Build HTML table
        table_rows = []
        for r in runs[:15]:
            report = r["report"]
            exec_status = (report.get("execution_result") or {}).get("status", "—")
            verified = (report.get("verification_result") or {}).get("passed")

            # Status dot
            if exec_status == "success":
                dot = status_dot(Colors.GREEN, "success")
            elif "rollback" in exec_status:
                dot = status_dot(Colors.YELLOW, "rollback")
            elif exec_status == "—":
                dot = status_dot(Colors.TEXT_SECONDARY, "pending")
            else:
                dot = status_dot(Colors.RED, exec_status[:12])

            # Score from eval
            run_score = "—"
            if eval_report:
                for res in eval_report.get("results", []):
                    if r["scenario"] in res.get("case_id", ""):
                        run_score = format_score(res.get("total_score", 0))
                        break

            # Cost
            cost_val = "—"
            if r["metrics_path"].exists():
                m = load_json(r["metrics_path"])
                if m:
                    c = (m.get("cost") or {}).get("total_cost")
                    if c is not None:
                        cost_val = format_cost(c)

            ts = r["generated_at"][:19] if r["generated_at"] else "—"
            table_rows.append([
                ts,
                f'<span style="color:{Colors.TEXT_PRIMARY}">{r["scenario"]}</span>',
                dot,
                run_score,
                cost_val,
            ])

        tbl = html_table(
            ["Time", "Scenario", "Status", "Score", "Cost"],
            table_rows,
            col_styles=["", "", "", "text-align:right", "text-align:right"],
        )
        st.markdown(tbl, unsafe_allow_html=True)

    with right_col:
        section_title("COST TREND")
        cost_data = []
        for r in reversed(runs[:20]):
            if r["metrics_path"].exists():
                m = load_json(r["metrics_path"])
                if m:
                    c = (m.get("cost") or {}).get("total_cost", 0)
                    cost_data.append({
                        "ts": r["generated_at"][:16] if r["generated_at"] else "",
                        "cost": c,
                        "exceeded": (m.get("cost") or {}).get("budget", {}).get("exceeded", False),
                    })

        if cost_data:
            import plotly.graph_objects as go

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=[d["ts"] for d in cost_data],
                y=[d["cost"] for d in cost_data],
                mode="lines",
                line=dict(color=Colors.BLUE, width=1.5),
                hovertemplate="%{x}<br>¥%{y:.4f}<extra></extra>",
            ))
            # Budget line
            fig.add_hline(
                y=0.1, line_dash="dot", line_color=Colors.RED, line_width=1,
                annotation_text="budget", annotation_font_color=Colors.RED,
                annotation_font_size=9,
            )
            fig.update_layout(
                **PLOTLY_LAYOUT,
                height=240,
                showlegend=False,
                xaxis_title=None,
                yaxis_title="CNY",
            )
            fig.update_xaxes(tickangle=-30, tickfont=dict(size=9))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            empty_state("No cost data available")

# ─── Agent status row ────────────────────────────────────────────────────────
st.markdown(f'<div style="border-bottom:1px solid {Colors.BORDER};margin:16px 0 12px 0;"></div>', unsafe_allow_html=True)

agents = ["AlertAgent", "RcaAgent", "PlannerAgent", "ExecutorAgent", "VerifierAgent"]
dots_html = "".join(
    f'<div class="agent-item">{status_dot(Colors.GREEN, name)}</div>'
    for name in agents
)
st.markdown(f'<div class="agent-row">{dots_html}</div>', unsafe_allow_html=True)
