# -*- coding: utf-8 -*-
"""OpsPilot Insight — Theme system.

Minimal, professional dark theme inspired by Grafana 8+, Datadog APM, PagerDuty.
No AI-demo aesthetics. Restrained color usage.
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st


# ─── Color constants ─────────────────────────────────────────────────────────
class Colors:
    BG_PAGE = "#111217"
    BG_PANEL = "#1a1d23"
    BG_ROW_ALT = "#1e2126"
    BG_ROW_HOVER = "#252830"
    BORDER = "#2a2d33"

    TEXT_PRIMARY = "#e0e0e0"
    TEXT_SECONDARY = "#8b8b8b"
    TEXT_WHITE = "#ffffff"

    # Status — used ONLY for small dots / thin borders
    GREEN = "#2ecc71"
    YELLOW = "#f39c12"
    RED = "#e74c3c"
    BLUE = "#3498db"

    CHART_PALETTE = ["#3498db", "#2ecc71", "#e67e22", "#9b59b6", "#1abc9c"]


# ─── Plotly shared layout ────────────────────────────────────────────────────
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#1a1d23",
    font=dict(color="#8b8b8b", size=11, family="Inter, -apple-system, sans-serif"),
    margin=dict(l=48, r=16, t=28, b=40),
    xaxis=dict(gridcolor="#2a2d33", zerolinecolor="#2a2d33"),
    yaxis=dict(gridcolor="#2a2d33", zerolinecolor="#2a2d33"),
    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#8b8b8b", size=10)),
    colorway=Colors.CHART_PALETTE,
)


# ─── Global CSS ──────────────────────────────────────────────────────────────
_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

.stApp {{
    background-color: {Colors.BG_PAGE};
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}}
header[data-testid="stHeader"] {{
    background-color: {Colors.BG_PAGE} !important;
    border-bottom: 1px solid {Colors.BORDER};
}}
section[data-testid="stSidebar"] {{
    background-color: {Colors.BG_PANEL};
    border-right: 1px solid {Colors.BORDER};
}}
section[data-testid="stSidebar"] .stMarkdown p {{
    color: {Colors.TEXT_SECONDARY};
    font-size: 0.8rem;
}}
footer {{visibility: hidden;}}

/* Remove default streamlit padding.
   顶部留出 4.5rem：Streamlit 的 stHeader 为 60px 高的绝对定位不透明顶栏
   （z-index 999990），padding 不足会导致自定义 .top-header 标题被其遮挡。 */
.block-container {{
    padding-top: 4.5rem !important;
    padding-bottom: 1rem !important;
}}

/* Section title */
.sec-title {{
    font-size: 11px;
    font-weight: 500;
    color: {Colors.TEXT_SECONDARY};
    text-transform: uppercase;
    letter-spacing: 1.2px;
    margin: 24px 0 12px 0;
    padding-bottom: 8px;
    border-bottom: 1px solid {Colors.BORDER};
}}

/* Stat cell — Grafana style */
.stat-cell {{
    padding: 16px 0;
}}
.stat-cell .stat-value {{
    font-size: 26px;
    font-weight: 600;
    color: {Colors.TEXT_WHITE};
    font-family: 'JetBrains Mono', 'SF Mono', monospace;
    line-height: 1.3;
}}
.stat-cell .stat-label {{
    font-size: 11px;
    color: {Colors.TEXT_SECONDARY};
    margin-top: 4px;
    letter-spacing: 0.3px;
}}

/* Custom HTML table */
.ops-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    font-family: 'Inter', sans-serif;
}}
.ops-table th {{
    text-align: left;
    padding: 8px 12px;
    color: {Colors.TEXT_SECONDARY};
    font-weight: 500;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid {Colors.BORDER};
    background: transparent;
}}
.ops-table td {{
    padding: 7px 12px;
    color: {Colors.TEXT_PRIMARY};
    border-bottom: 1px solid #1e2126;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
}}
.ops-table tr:nth-child(even) td {{
    background: {Colors.BG_ROW_ALT};
}}
.ops-table tr:nth-child(odd) td {{
    background: {Colors.BG_PANEL};
}}
.ops-table tr:hover td {{
    background: {Colors.BG_ROW_HOVER};
}}

/* Status dot */
.s-dot {{
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
}}

/* Header bar */
.top-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 0 16px 0;
    border-bottom: 1px solid {Colors.BORDER};
    margin-bottom: 20px;
}}
.top-header .logo {{
    font-size: 14px;
    font-weight: 600;
    color: {Colors.TEXT_PRIMARY};
    letter-spacing: -0.3px;
}}
.top-header .ts {{
    font-size: 11px;
    color: {Colors.TEXT_SECONDARY};
    font-family: 'JetBrains Mono', monospace;
}}

/* Agent status row */
.agent-row {{
    display: flex;
    gap: 24px;
    align-items: center;
    padding: 12px 0;
}}
.agent-row .agent-item {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: {Colors.TEXT_PRIMARY};
}}

/* Trace span row */
.span-row {{
    display: flex;
    align-items: center;
    padding: 4px 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
}}
.span-row .tree-line {{
    color: #3a3d44;
    white-space: pre;
    user-select: none;
}}
.span-row .span-name {{
    color: {Colors.TEXT_PRIMARY};
    flex-shrink: 0;
    margin-right: 12px;
}}
.span-row .dur-bar {{
    height: 4px;
    border-radius: 1px;
    min-width: 2px;
    flex-shrink: 0;
}}
.span-row .dur-val {{
    color: {Colors.TEXT_SECONDARY};
    font-size: 11px;
    min-width: 64px;
    text-align: right;
    margin-left: auto;
    flex-shrink: 0;
}}

/* Info bar (selector bar) */
.info-bar {{
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 10px 16px;
    background: {Colors.BG_PANEL};
    border: 1px solid {Colors.BORDER};
    border-radius: 2px;
    margin-bottom: 16px;
    font-size: 12px;
    color: {Colors.TEXT_SECONDARY};
}}
.info-bar .ib-val {{
    color: {Colors.TEXT_WHITE};
    font-family: 'JetBrains Mono', monospace;
}}

/* Progress bar thin */
.pbar {{
    background: #252830;
    height: 4px;
    border-radius: 2px;
    overflow: hidden;
    width: 100%;
    margin-top: 4px;
}}
.pbar-fill {{
    height: 100%;
    border-radius: 2px;
}}

/* Expander override */
.streamlit-expanderHeader {{
    font-size: 12px !important;
    color: {Colors.TEXT_PRIMARY} !important;
    background: {Colors.BG_PANEL} !important;
}}

/* Empty state */
.empty-state {{
    text-align: center;
    padding: 48px 20px;
    color: {Colors.TEXT_SECONDARY};
    font-size: 12px;
}}
</style>
"""


def inject_css():
    """Inject global CSS. Call once per page."""
    st.markdown(_CSS, unsafe_allow_html=True)


# ─── Component functions ─────────────────────────────────────────────────────

def render_header():
    """Top header bar with logo and timestamp."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.markdown(f"""
    <div class="top-header">
        <div class="logo">OpsPilot Insight</div>
        <div class="ts">Last updated: {now}</div>
    </div>
    """, unsafe_allow_html=True)


def section_title(text: str):
    """12px uppercase gray section title."""
    st.markdown(f'<div class="sec-title">{text}</div>', unsafe_allow_html=True)


def stat_cell(value, label: str):
    """Single stat cell — white number + gray label. Grafana stat panel style."""
    st.markdown(f"""
    <div class="stat-cell">
        <div class="stat-value">{value}</div>
        <div class="stat-label">{label}</div>
    </div>
    """, unsafe_allow_html=True)


def status_dot(color: str, text: str) -> str:
    """Inline HTML: 6px colored dot + text label."""
    return f'<span class="s-dot" style="background:{color};"></span><span style="color:{Colors.TEXT_PRIMARY};font-size:12px;">{text}</span>'


def html_table(headers: list, rows: list[list], col_styles: list[str] | None = None) -> str:
    """Build a professional HTML table string.
    
    headers: list of column header strings
    rows: list of row lists (each row = list of cell HTML strings)
    col_styles: optional per-column inline styles
    """
    ths = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        tds = ""
        for i, cell in enumerate(row):
            style = col_styles[i] if col_styles and i < len(col_styles) else ""
            tds += f'<td style="{style}">{cell}</td>'
        body += f"<tr>{tds}</tr>"
    return f'<table class="ops-table"><thead><tr>{ths}</tr></thead><tbody>{body}</tbody></table>'


def empty_state(msg: str):
    """Empty state placeholder."""
    st.markdown(f'<div class="empty-state">{msg}</div>', unsafe_allow_html=True)


def format_cost(v) -> str:
    """Format cost to 4 decimal places."""
    if isinstance(v, (int, float)):
        return f"¥{v:.4f}"
    return "—"


def format_score(v) -> str:
    """Format score: integer if whole, 1 decimal otherwise."""
    if isinstance(v, (int, float)):
        if v == int(v):
            return str(int(v))
        return f"{v:.1f}"
    return "—"
