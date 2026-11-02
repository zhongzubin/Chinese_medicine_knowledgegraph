# -*- coding: utf-8 -*-
"""知识图谱问答 Streamlit 交互工作台。"""

import html
import json
import os
from typing import Any, Iterator, Optional, Sequence, Tuple

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


SUGGESTED_QUESTIONS = (
    "麻黄有什么功效？",
    "四君子汤由哪些药材组成？",
    "含有人参的方剂有多少种？",
    "同时含有人参和白术的方剂有哪些？",
)

PLAN_LABELS = {
    "controlled_multi_hop": "受控两跳",
    "controlled_attribute": "图谱属性",
    "controlled_aggregate": "受控统计",
    "controlled_multi_condition_list": "多条件列表",
    "controlled_relationship": "受控关系",
    "schema_constrained_cypher": "Schema 约束查询",
    "rejected": "规划校验未通过",
}

ENTITY_LABELS = {
    "Herb": "药材",
    "Formula": "方剂",
    "Disease": "疾病",
    "Symptom": "症状",
    "Effect": "功效",
    "Source": "文献",
}

APP_CSS = """
<style>
:root {
    --kg-green: #1f6b54;
    --kg-green-dark: #174d3e;
    --kg-green-soft: #e7f1ec;
    --kg-gold: #b8822f;
    --kg-gold-soft: #f7ecd5;
    --kg-ink: #24352e;
    --kg-muted: #6b7972;
    --kg-line: rgba(31, 107, 84, 0.16);
    --kg-surface: rgba(255, 255, 255, 0.9);
    --kg-background: #f6f2e8;
}

.stApp {
    background:
        radial-gradient(circle at 8% 2%, rgba(184, 130, 47, 0.11), transparent 26rem),
        radial-gradient(circle at 92% 0%, rgba(31, 107, 84, 0.12), transparent 30rem),
        linear-gradient(180deg, #f8f5ed 0%, var(--kg-background) 58%, #f7f4ec 100%);
    color: var(--kg-ink);
}

[data-testid="stHeader"] {
    background: transparent;
}

[data-testid="stToolbar"], #MainMenu, footer {
    visibility: hidden;
}

.block-container {
    max-width: 1240px;
    padding-top: 2.1rem;
    padding-bottom: 8.2rem;
}

.kg-hero {
    position: relative;
    overflow: hidden;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 2rem;
    min-height: 190px;
    padding: 1.85rem 2.5rem;
    margin-bottom: 1.25rem;
    border: 1px solid rgba(31, 107, 84, 0.14);
    border-radius: 28px;
    background:
        linear-gradient(120deg, rgba(255, 255, 255, 0.94), rgba(244, 249, 246, 0.88)),
        repeating-linear-gradient(135deg, rgba(31, 107, 84, 0.025) 0 1px, transparent 1px 12px);
    box-shadow: 0 24px 60px rgba(39, 61, 50, 0.08);
}

.kg-hero::after {
    content: "";
    position: absolute;
    right: -5rem;
    bottom: -9rem;
    width: 25rem;
    height: 25rem;
    border: 1px solid rgba(184, 130, 47, 0.16);
    border-radius: 50%;
    box-shadow:
        0 0 0 28px rgba(184, 130, 47, 0.035),
        0 0 0 58px rgba(31, 107, 84, 0.025);
    pointer-events: none;
}

.kg-hero-copy {
    position: relative;
    z-index: 1;
    max-width: 790px;
}

.kg-kicker {
    display: inline-block;
    margin-bottom: 0.85rem;
    color: var(--kg-gold);
    font-size: 0.73rem;
    font-weight: 800;
    letter-spacing: 0.2em;
}

.kg-title {
    margin: 0;
    color: var(--kg-green-dark);
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: clamp(2.2rem, 4vw, 3.55rem);
    font-weight: 800;
    letter-spacing: -0.04em;
    line-height: 1.12;
}

.kg-subtitle {
    max-width: 720px;
    margin: 0.9rem 0 0;
    color: var(--kg-muted);
    font-size: 1rem;
    line-height: 1.8;
}

.kg-seal {
    position: relative;
    z-index: 1;
    display: grid;
    place-items: center;
    flex: 0 0 116px;
    width: 116px;
    height: 116px;
    border: 1px solid rgba(31, 107, 84, 0.3);
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.68);
    color: var(--kg-green);
    box-shadow: inset 0 0 0 8px rgba(31, 107, 84, 0.04);
}

.kg-seal strong {
    font-family: "STKaiti", "KaiTi", serif;
    font-size: 2.7rem;
    font-weight: 700;
    line-height: 1;
}

.kg-seal small {
    margin-top: -0.45rem;
    color: var(--kg-gold);
    font-size: 0.66rem;
    letter-spacing: 0.18em;
}

.kg-status-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.55rem;
    min-height: 2.45rem;
}

.kg-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.36rem;
    padding: 0.38rem 0.72rem;
    border: 1px solid rgba(31, 107, 84, 0.15);
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.65);
    color: #526159;
    font-size: 0.76rem;
    font-weight: 650;
    white-space: nowrap;
}

.kg-chip-dot {
    width: 0.42rem;
    height: 0.42rem;
    border-radius: 50%;
    background: var(--kg-green);
    box-shadow: 0 0 0 3px rgba(31, 107, 84, 0.1);
}

.kg-session {
    margin-left: auto;
    color: #7b877f;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 0.72rem;
}

.kg-flash {
    margin: 0.7rem 0 0.35rem;
    padding: 0.78rem 0.95rem;
    border: 1px solid rgba(184, 130, 47, 0.22);
    border-radius: 14px;
    background: rgba(247, 236, 213, 0.78);
    color: #76531d;
    font-size: 0.86rem;
}

.kg-question-card {
    display: flex;
    align-items: flex-start;
    gap: 0.9rem;
    margin: 1.1rem 0 1rem;
    padding: 1rem 1.15rem;
    border: 1px solid rgba(31, 107, 84, 0.13);
    border-left: 4px solid var(--kg-green);
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.78);
    box-shadow: 0 10px 30px rgba(39, 61, 50, 0.04);
}

.kg-question-label {
    flex: 0 0 auto;
    padding: 0.2rem 0.5rem;
    border-radius: 8px;
    background: var(--kg-green-soft);
    color: var(--kg-green);
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.08em;
}

.kg-question-text {
    color: var(--kg-ink);
    font-size: 1rem;
    font-weight: 650;
    line-height: 1.65;
}

.kg-panel-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.65rem;
}

.kg-panel-heading strong {
    color: var(--kg-ink);
    font-size: 1.02rem;
}

.kg-panel-heading span {
    color: #849087;
    font-size: 0.72rem;
}

.kg-timeline {
    display: grid;
    gap: 0.14rem;
    padding: 0.2rem 0 0.4rem;
}

.kg-timeline-item {
    position: relative;
    display: grid;
    grid-template-columns: 2.05rem minmax(0, 1fr);
    align-items: center;
    min-height: 2.25rem;
    color: #46564e;
    font-size: 0.86rem;
}

.kg-timeline-item:not(:last-child)::after {
    content: "";
    position: absolute;
    left: 0.74rem;
    top: 1.7rem;
    bottom: -0.72rem;
    width: 1px;
    background: rgba(31, 107, 84, 0.16);
}

.kg-timeline-index {
    position: relative;
    z-index: 1;
    display: grid;
    place-items: center;
    width: 1.5rem;
    height: 1.5rem;
    border: 1px solid rgba(31, 107, 84, 0.2);
    border-radius: 50%;
    background: #f8faf8;
    color: var(--kg-green);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 0.62rem;
    font-weight: 800;
}

.kg-timeline-item.active .kg-timeline-index {
    border-color: var(--kg-gold);
    background: var(--kg-gold-soft);
    color: #8b6020;
    box-shadow: 0 0 0 4px rgba(184, 130, 47, 0.08);
}

.kg-timeline-text {
    overflow-wrap: anywhere;
    line-height: 1.55;
}

.kg-timeline-running {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    padding: 0.8rem 0;
    color: var(--kg-muted);
    font-size: 0.86rem;
}

.kg-pulse {
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    background: var(--kg-green);
    box-shadow: 0 0 0 0 rgba(31, 107, 84, 0.28);
    animation: kg-pulse 1.55s infinite;
}

@keyframes kg-pulse {
    0% { box-shadow: 0 0 0 0 rgba(31, 107, 84, 0.28); }
    70% { box-shadow: 0 0 0 9px rgba(31, 107, 84, 0); }
    100% { box-shadow: 0 0 0 0 rgba(31, 107, 84, 0); }
}

.kg-answer-copy {
    color: #314139;
    font-size: 0.98rem;
    line-height: 1.85;
}

.kg-answer-copy h1,
.kg-answer-copy h2,
.kg-answer-copy h3 {
    color: var(--kg-green-dark);
}

.kg-empty-panel {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 1.2rem;
    align-items: center;
    margin-top: 1.3rem;
    padding: 1.65rem 1.7rem;
    border: 1px solid rgba(31, 107, 84, 0.13);
    border-radius: 24px;
    background:
        linear-gradient(125deg, rgba(255, 255, 255, 0.93), rgba(241, 248, 244, 0.86));
    box-shadow: 0 20px 55px rgba(39, 61, 50, 0.06);
}

.kg-empty-icon {
    display: grid;
    place-items: center;
    width: 5rem;
    height: 5rem;
    border: 1px solid rgba(31, 107, 84, 0.18);
    border-radius: 22px;
    background: var(--kg-green-soft);
    color: var(--kg-green);
    font-family: "STKaiti", "KaiTi", serif;
    font-size: 2.25rem;
    font-weight: 700;
}

.kg-empty-panel h2 {
    margin: 0 0 0.35rem;
    color: var(--kg-green-dark);
    font-size: 1.35rem;
}

.kg-empty-panel p {
    margin: 0;
    color: var(--kg-muted);
    font-size: 0.9rem;
    line-height: 1.7;
}

.kg-section-kicker {
    margin: 1.4rem 0 0.5rem;
    color: var(--kg-gold);
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.18em;
}

.kg-feature-card {
    min-height: 118px;
    padding: 1rem 1.05rem;
    border: 1px solid rgba(31, 107, 84, 0.1);
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.62);
}

.kg-feature-card span {
    color: var(--kg-gold);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.12em;
}

.kg-feature-card strong {
    display: block;
    margin-top: 0.45rem;
    color: var(--kg-ink);
    font-size: 0.92rem;
}

.kg-feature-card p {
    margin: 0.35rem 0 0;
    color: var(--kg-muted);
    font-size: 0.78rem;
    line-height: 1.55;
}

.kg-audit-banner {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.9rem 1rem;
    border: 1px solid rgba(31, 107, 84, 0.17);
    border-radius: 14px;
    background: rgba(231, 241, 236, 0.75);
}

.kg-audit-banner.fallback {
    border-color: rgba(184, 130, 47, 0.24);
    background: rgba(247, 236, 213, 0.78);
}

.kg-audit-mark {
    display: grid;
    place-items: center;
    flex: 0 0 1.65rem;
    width: 1.65rem;
    height: 1.65rem;
    border-radius: 50%;
    background: var(--kg-green);
    color: #ffffff;
    font-size: 0.78rem;
    font-weight: 800;
}

.kg-audit-banner.fallback .kg-audit-mark {
    background: var(--kg-gold);
}

.kg-audit-message strong {
    display: block;
    margin-bottom: 0.18rem;
    color: var(--kg-ink);
    font-size: 0.9rem;
}

.kg-audit-message span {
    color: var(--kg-muted);
    font-size: 0.8rem;
    line-height: 1.55;
}

.kg-evidence-list {
    display: grid;
    gap: 0.7rem;
    max-height: 540px;
    overflow-y: auto;
    padding: 0.15rem 0.15rem 0.4rem;
}

.kg-evidence-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    gap: 0.8rem;
    align-items: center;
    padding: 0.85rem 0.95rem;
    border: 1px solid rgba(31, 107, 84, 0.11);
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.7);
}

.kg-evidence-node {
    min-width: 0;
}

.kg-evidence-label {
    display: inline-block;
    margin-bottom: 0.22rem;
    padding: 0.13rem 0.4rem;
    border-radius: 6px;
    background: var(--kg-green-soft);
    color: var(--kg-green);
    font-size: 0.64rem;
    font-weight: 750;
}

.kg-evidence-name {
    display: block;
    overflow-wrap: anywhere;
    color: var(--kg-ink);
    font-size: 0.88rem;
    font-weight: 700;
}

.kg-evidence-edge {
    color: var(--kg-gold);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 0.7rem;
    font-weight: 700;
    text-align: center;
}

.kg-evidence-source {
    grid-column: 1 / -1;
    padding-top: 0.5rem;
    border-top: 1px dashed rgba(31, 107, 84, 0.12);
    color: #7c8981;
    font-size: 0.72rem;
}

.kg-fact-card,
.kg-aggregate-card,
.kg-path-card {
    padding: 0.9rem 1rem;
    border: 1px solid rgba(31, 107, 84, 0.11);
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.7);
}

.kg-fact-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
}

.kg-fact-header strong {
    color: var(--kg-ink);
    font-size: 0.9rem;
}

.kg-fact-header span {
    color: var(--kg-green);
    font-size: 0.72rem;
    font-weight: 750;
}

.kg-fact-value {
    margin: 0.65rem 0;
    color: #394a41;
    font-size: 0.86rem;
    line-height: 1.7;
    white-space: pre-wrap;
}

.kg-fact-source {
    color: #7d8982;
    font-size: 0.72rem;
}

.kg-aggregate-card {
    display: flex;
    align-items: center;
    gap: 1rem;
}

.kg-aggregate-count {
    flex: 0 0 auto;
    color: var(--kg-green);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 2rem;
    font-weight: 800;
}

.kg-aggregate-copy strong {
    display: block;
    color: var(--kg-ink);
    font-size: 0.88rem;
}

.kg-aggregate-copy span {
    color: var(--kg-muted);
    font-size: 0.76rem;
}

.kg-path-chain {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
    margin-bottom: 0.55rem;
}

.kg-path-node {
    padding: 0.42rem 0.55rem;
    border: 1px solid rgba(31, 107, 84, 0.12);
    border-radius: 9px;
    background: rgba(231, 241, 236, 0.58);
    color: var(--kg-ink);
    font-size: 0.8rem;
    font-weight: 650;
}

.kg-path-relation {
    color: var(--kg-gold);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 0.68rem;
    font-weight: 700;
}

[data-testid="stVerticalBlockBorderWrapper"] {
    border-color: rgba(31, 107, 84, 0.14) !important;
    border-radius: 20px !important;
    background: var(--kg-surface);
    box-shadow: 0 16px 42px rgba(39, 61, 50, 0.055);
}

[data-testid="stForm"] {
    padding: 0;
    border: 0;
    background: transparent;
}

[data-testid="stTextInput"] input {
    min-height: 3.15rem;
    padding: 0 1rem;
    border: 1px solid rgba(31, 107, 84, 0.22) !important;
    border-radius: 16px !important;
    background: rgba(255, 255, 255, 0.95) !important;
    color: var(--kg-ink) !important;
    box-shadow: 0 16px 40px rgba(39, 61, 50, 0.08);
}

[data-testid="stTextInput"] input:focus {
    border-color: var(--kg-green) !important;
    box-shadow: 0 0 0 3px rgba(31, 107, 84, 0.1);
}

.stFormSubmitButton button {
    min-height: 3.15rem !important;
    border-color: var(--kg-green) !important;
    background: var(--kg-green) !important;
    color: #ffffff !important;
    font-weight: 750 !important;
    box-shadow: 0 12px 24px rgba(31, 107, 84, 0.16) !important;
}

.stFormSubmitButton button:hover {
    border-color: var(--kg-green-dark) !important;
    background: var(--kg-green-dark) !important;
    color: #ffffff !important;
}

.stButton > button {
    min-height: 2.65rem;
    border: 1px solid rgba(31, 107, 84, 0.16);
    border-radius: 12px;
    background: rgba(255, 255, 255, 0.75);
    color: var(--kg-green-dark);
    font-weight: 650;
    box-shadow: none;
    transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
}

.stButton > button:hover {
    border-color: var(--kg-green);
    background: #ffffff;
    color: var(--kg-green);
    transform: translateY(-1px);
}

.stButton > button:disabled {
    border-color: rgba(31, 107, 84, 0.08);
    background: rgba(255, 255, 255, 0.38);
    color: #9aa49e;
}

button[data-baseweb="tab"] {
    border-radius: 10px;
    color: var(--kg-muted);
    font-weight: 650;
}

button[data-baseweb="tab"][aria-selected="true"] {
    background: var(--kg-green-soft);
    color: var(--kg-green-dark);
}

[data-testid="stMetric"] {
    padding: 0.7rem 0.8rem;
    border: 1px solid rgba(31, 107, 84, 0.09);
    border-radius: 13px;
    background: rgba(248, 251, 249, 0.76);
}

[data-testid="stMetricLabel"] {
    color: #7a877f;
}

[data-testid="stMetricValue"] {
    color: var(--kg-green-dark);
}

@media (max-width: 760px) {
    .block-container {
        padding-top: 1rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }
    .kg-hero {
        min-height: auto;
        padding: 1.45rem 1.25rem;
        border-radius: 20px;
    }
    .kg-title {
        font-size: 2rem;
        letter-spacing: -0.05em;
        white-space: nowrap;
    }
    .kg-subtitle {
        font-size: 0.9rem;
        line-height: 1.7;
    }
    .kg-seal {
        display: none;
    }
    .kg-session {
        width: 100%;
        margin-left: 0;
    }
    .kg-empty-panel {
        grid-template-columns: 1fr;
    }
    .kg-evidence-row {
        grid-template-columns: 1fr;
    }
    .kg-evidence-edge {
        text-align: left;
    }
}
</style>
"""

HERO_HTML = """
<section class="kg-hero" aria-label="中医知识图谱问答">
    <div class="kg-hero-copy">
        <span class="kg-kicker">VERIFIABLE TCM GRAPH QA</span>
        <h1 class="kg-title">中医知识图谱问答</h1>
        <p class="kg-subtitle">
            从真实知识图谱中查找事实，以受控查询约束检索过程，
            让回答中的关系、属性、统计和路径都有据可核。
        </p>
    </div>
    <div class="kg-seal" aria-hidden="true">
        <strong>证</strong>
        <small>可追溯</small>
    </div>
</section>
"""


def parse_sse_lines(lines: Iterator[object]) -> Iterator[Tuple[str, dict]]:
    event_name: Optional[str] = None
    data_lines = []
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = str(line)
        if not line:
            if event_name and data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {}
                yield event_name, payload
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())


def _answer_markdown(answer: str) -> str:
    return answer or "正在整理图谱证据并生成回答…"


def _stage_display_label(event_data: dict) -> Optional[str]:
    stage_label = event_data.get("label")
    plan_label = event_data.get("plan_label")
    if (
        event_data.get("stage") == "plan_cypher"
        and stage_label
        and plan_label
    ):
        return f"{stage_label}（{plan_label}）"
    if event_data.get("stage") == "retrieve_observe" and stage_label:
        decision_labels = {
            "replan": "重新规划",
            "answer_from_graph": "进入证据回答",
            "no_evidence": "无可用依据",
            "safe_fail": "安全终止",
            "finish": "检索结束",
        }
        decision_label = decision_labels.get(
            event_data.get("decision"),
        )
        if decision_label:
            return f"{stage_label}（{decision_label}）"
    return stage_label


def _compact_session_id(session_id: Optional[str]) -> str:
    if not session_id:
        return "尚未建立会话"
    compact = session_id.replace("-", "")
    return f"会话 · {compact[:8]}"


def _evidence_tab_specs(
    evidence: Sequence[dict],
    facts: Sequence[dict],
    aggregates: Sequence[dict],
    paths: Sequence[dict],
) -> list[Tuple[str, str, int]]:
    candidates = (
        ("relations", "关系证据", evidence),
        ("facts", "属性事实", facts),
        ("aggregates", "图谱统计", aggregates),
        ("paths", "多跳路径", paths),
    )
    return [
        (key, label, len(items))
        for key, label, items in candidates
        if items
    ]


def _verification_ratio(verification: Optional[dict]) -> float:
    if not verification:
        return 0.0
    checked = verification.get("checked_items", 0)
    if not isinstance(checked, int) or checked <= 0:
        return 0.0
    matched = verification.get("matched_items", 0)
    if not isinstance(matched, int):
        return 0.0
    return max(0.0, min(1.0, matched / checked))


def _plan_label(plan_kind: Optional[str]) -> Optional[str]:
    if not plan_kind:
        return None
    return PLAN_LABELS.get(plan_kind, plan_kind)


def _progress_timeline_html(
    labels: Sequence[str],
    *,
    running: bool,
) -> str:
    if not labels:
        message = "正在连接问答服务…" if running else "暂无处理记录"
        pulse = '<span class="kg-pulse"></span>' if running else ""
        return (
            '<div class="kg-timeline-running">'
            f"{pulse}<span>{html.escape(message)}</span>"
            "</div>"
        )
    items = []
    for index, label in enumerate(labels, start=1):
        active = running and index == len(labels)
        class_name = "kg-timeline-item active" if active else "kg-timeline-item"
        items.append(
            f'<div class="{class_name}">'
            f'<span class="kg-timeline-index">{index:02d}</span>'
            f'<span class="kg-timeline-text">{html.escape(label)}</span>'
            "</div>"
        )
    return '<div class="kg-timeline">' + "".join(items) + "</div>"


def _inject_styles() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)


def _render_status_bar(
    session_id: Optional[str],
    api_url: str,
    api_key: str,
) -> None:
    info_column, action_column = st.columns([5.2, 1.05])
    with info_column:
        session_label = html.escape(_compact_session_id(session_id))
        st.markdown(
            (
                '<div class="kg-status-strip">'
                '<span class="kg-chip"><span class="kg-chip-dot"></span>'
                "只读 Neo4j</span>"
                '<span class="kg-chip"><span class="kg-chip-dot"></span>'
                "证据审核</span>"
                '<span class="kg-chip"><span class="kg-chip-dot"></span>'
                "最近 5 轮会话</span>"
                f'<span class="kg-session">{session_label}</span>'
                "</div>"
            ),
            unsafe_allow_html=True,
        )
    with action_column:
        if st.button(
            "清空当前会话",
            use_container_width=True,
            disabled=not bool(session_id),
        ):
            if session_id:
                try:
                    requests.delete(
                        f"{api_url}/api/v1/sessions/{session_id}",
                        headers={"X-API-Key": api_key},
                        timeout=10,
                    )
                except requests.RequestException:
                    st.session_state.flash_message = (
                        "本地页面已重置，但未能确认服务端会话已经删除。"
                    )
            _reset_local_conversation()
            st.rerun()


def _reset_local_conversation() -> None:
    st.session_state.session_id = None
    st.session_state.progress_labels = []
    st.session_state.last_result = None
    st.session_state.pending_question = None


def _render_question_card(question: str) -> None:
    st.markdown(
        (
            '<div class="kg-question-card">'
            '<span class="kg-question-label">本轮问题</span>'
            f'<span class="kg-question-text">{html.escape(question)}</span>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _render_panel_heading(title: str, caption: str) -> None:
    st.markdown(
        (
            '<div class="kg-panel-heading">'
            f"<strong>{html.escape(title)}</strong>"
            f"<span>{html.escape(caption)}</span>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _render_question_form() -> Tuple[str, bool]:
    with st.form("graph_question_form", clear_on_submit=True):
        input_column, submit_column = st.columns([5.4, 1.1])
        with input_column:
            question = st.text_input(
                "问题",
                label_visibility="collapsed",
                placeholder=(
                    "输入一味药、一首方剂，或一个具体的中医知识问题…"
                ),
                key="question_input",
            )
        with submit_column:
            submitted = st.form_submit_button(
                "开始溯源",
                use_container_width=True,
            )
    return question.strip(), submitted


def _render_empty_state() -> None:
    st.markdown(
        """
        <section class="kg-empty-panel">
            <div class="kg-empty-icon">问</div>
            <div>
                <h2>从一个具体问题开始</h2>
                <p>
                    可以询问一味药材、一首方剂、疾病症状、组成关系，
                    或“同时满足多个条件”的图谱问题。系统会在没有可靠依据时明确说明。
                </p>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="kg-section-kicker">HOW IT WORKS</div>',
        unsafe_allow_html=True,
    )
    feature_columns = st.columns(3, gap="small")
    features = (
        ("01", "真实图谱", "事实来自 Neo4j 只读查询，而不是模型常识。"),
        ("02", "受控检索", "优先使用属性、关系和 Schema 模板，减少随机查询。"),
        ("03", "逐项审核", "回答必须覆盖本轮图谱证据，不足时自动回退。"),
    )
    for column, (number, title, description) in zip(
        feature_columns,
        features,
        strict=True,
    ):
        with column:
            st.markdown(
                (
                    '<div class="kg-feature-card">'
                    f"<span>{number}</span>"
                    f"<strong>{title}</strong>"
                    f"<p>{description}</p>"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )

    st.markdown(
        '<div class="kg-section-kicker">TRY A QUESTION</div>',
        unsafe_allow_html=True,
    )
    prompt_columns = st.columns(2, gap="small")
    for index, prompt in enumerate(SUGGESTED_QUESTIONS):
        with prompt_columns[index % 2]:
            if st.button(
                prompt,
                key=f"suggested_question_{index}",
                use_container_width=True,
            ):
                st.session_state.pending_question = prompt
                st.rerun()


def _render_stream_shell(
    question: str,
) -> Tuple[Any, Any]:
    _render_question_card(question)
    left, right = st.columns([0.82, 1.75], gap="large")
    with left:
        with st.container(border=True):
            _render_panel_heading("处理轨迹", "公开阶段")
            progress_placeholder = st.empty()
            progress_placeholder.markdown(
                _progress_timeline_html((), running=True),
                unsafe_allow_html=True,
            )
            st.caption("仅展示公开处理阶段与检索决策，不展示模型隐藏推理。")
    with right:
        with st.container(border=True):
            _render_panel_heading("回答", "实时生成")
            answer_placeholder = st.empty()
            answer_placeholder.markdown(_answer_markdown(""))
    return progress_placeholder, answer_placeholder


def _run_query(
    question: str,
    *,
    api_url: str,
    api_key: str,
    session_id: Optional[str],
    progress_placeholder: Any,
    answer_placeholder: Any,
) -> dict:
    result = {
        "question": question,
        "answer": "",
        "grounded": False,
        "progress_labels": [],
        "evidence": [],
        "facts": [],
        "aggregates": [],
        "paths": [],
        "verification": None,
        "warnings": [],
        "error": None,
        "plan_kind": None,
        "session_id": session_id,
    }
    payload = {"question": question}
    if session_id:
        payload["session_id"] = session_id

    try:
        with requests.post(
            f"{api_url}/api/v1/qa/stream",
            headers={
                "X-API-Key": api_key,
                "Accept": "text/event-stream",
            },
            json=payload,
            stream=True,
            timeout=(10, 180),
        ) as response:
            if response.status_code != 200:
                try:
                    detail = response.json()
                except ValueError:
                    detail = {}
                error = detail.get("error") or {
                    "message": "问答服务返回异常状态"
                }
                result["error"] = error
                answer_placeholder.error(
                    error.get("message", "问答服务调用失败")
                )
                return result

            for event_name, event_data in parse_sse_lines(
                response.iter_lines(decode_unicode=True)
            ):
                if event_name == "stage":
                    display_label = _stage_display_label(event_data)
                    labels = result["progress_labels"]
                    if display_label and display_label not in labels:
                        labels.append(display_label)
                        progress_placeholder.markdown(
                            _progress_timeline_html(
                                labels,
                                running=True,
                            ),
                            unsafe_allow_html=True,
                        )
                elif event_name == "answer_delta":
                    result["answer"] += event_data.get("delta", "")
                    answer_placeholder.markdown(
                        _answer_markdown(result["answer"])
                    )
                elif event_name == "evidence":
                    result["evidence"] = event_data.get("items", [])
                elif event_name == "facts":
                    result["facts"] = event_data.get("items", [])
                elif event_name == "aggregates":
                    result["aggregates"] = event_data.get("items", [])
                elif event_name == "paths":
                    result["paths"] = event_data.get("items", [])
                elif event_name == "audit":
                    result["verification"] = event_data
                elif event_name == "warning":
                    message = event_data.get("message")
                    if message and message not in result["warnings"]:
                        result["warnings"].append(message)
                elif event_name == "error":
                    result["error"] = event_data
                    answer_placeholder.error(
                        event_data.get("message", "问答处理失败")
                    )
                elif event_name == "done":
                    done_response = event_data.get("response", {}) or {}
                    result["session_id"] = done_response.get(
                        "session_id",
                        result["session_id"],
                    )
                    final_answer = done_response.get("answer")
                    if final_answer:
                        result["answer"] = final_answer
                        answer_placeholder.markdown(final_answer)
                    result["grounded"] = bool(
                        done_response.get("grounded", False)
                    )
                    if done_response.get("verification"):
                        result["verification"] = done_response["verification"]
                    if done_response.get("error"):
                        result["error"] = done_response["error"]
                    done_warnings = done_response.get("warnings") or []
                    for warning in done_warnings:
                        if warning not in result["warnings"]:
                            result["warnings"].append(warning)
                    query = done_response.get("query") or {}
                    result["plan_kind"] = query.get("plan_kind")
    except (requests.RequestException, RuntimeError) as exc:
        result["error"] = {
            "message": str(exc) or "问答服务连接失败",
        }
        answer_placeholder.error(str(exc))

    progress_placeholder.markdown(
        _progress_timeline_html(
            result["progress_labels"],
            running=False,
        ),
        unsafe_allow_html=True,
    )
    return result


def _render_metrics(result: dict) -> None:
    evidence_count = sum(
        len(result.get(key, []))
        for key in ("evidence", "facts", "aggregates", "paths")
    )
    verification = result.get("verification") or {}
    verification_status = verification.get("status")
    audit_label = {
        "passed": "已通过",
        "fallback": "证据回退",
    }.get(verification_status, "未审核")
    missing_sources = verification.get("missing_sources", 0)
    metric_columns = st.columns(4, gap="small")
    metrics = (
        ("图谱依据", "已使用" if result.get("grounded") else "未使用"),
        ("可验证证据", str(evidence_count)),
        ("回答审核", audit_label),
        (
            "来源缺失",
            str(missing_sources) if verification else "未统计",
        ),
    )
    for column, (label, value) in zip(metric_columns, metrics, strict=True):
        with column:
            st.metric(label, value)
    plan_label = _plan_label(result.get("plan_kind"))
    if plan_label:
        st.caption(f"检索策略 · {plan_label}")


def _source_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return html.escape(value.strip())
    return "当前图谱未提供出处"


def _entity_label(value: Any) -> str:
    text = str(value)
    return html.escape(ENTITY_LABELS.get(text, text))


def _render_relation_evidence(items: Sequence[dict]) -> None:
    rows = []
    for item in items:
        rows.append(
            '<div class="kg-evidence-row">'
            '<div class="kg-evidence-node">'
            f'<span class="kg-evidence-label">{_entity_label(item.get("subject_label", ""))}</span>'
            f'<strong class="kg-evidence-name">{html.escape(str(item.get("subject", "")))}</strong>'
            "</div>"
            f'<div class="kg-evidence-edge">{html.escape(str(item.get("relationship", "")))} →</div>'
            '<div class="kg-evidence-node">'
            f'<span class="kg-evidence-label">{_entity_label(item.get("object_label", ""))}</span>'
            f'<strong class="kg-evidence-name">{html.escape(str(item.get("object", "")))}</strong>'
            "</div>"
            f'<div class="kg-evidence-source">来源 · {_source_text(item.get("source"))}</div>'
            "</div>"
        )
    st.markdown(
        '<div class="kg-evidence-list">' + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )


def _render_fact_evidence(items: Sequence[dict]) -> None:
    cards = []
    for item in items:
        cards.append(
            '<div class="kg-fact-card">'
            '<div class="kg-fact-header">'
            f'<strong>{html.escape(str(item.get("subject", "")))}</strong>'
            f'<span>{html.escape(str(item.get("attribute_label", "")))}</span>'
            "</div>"
            f'<div class="kg-fact-value">{html.escape(str(item.get("value", "")))}</div>'
            f'<div class="kg-fact-source">来源 · {_source_text(item.get("source"))}</div>'
            "</div>"
        )
    st.markdown(
        '<div class="kg-evidence-list">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def _render_aggregate_evidence(items: Sequence[dict]) -> None:
    cards = []
    for item in items:
        cards.append(
            '<div class="kg-aggregate-card">'
            f'<div class="kg-aggregate-count">{int(item.get("count", 0))}</div>'
            '<div class="kg-aggregate-copy">'
            f'<strong>{html.escape(str(item.get("description", "")))}</strong>'
            f'<span>{_entity_label(item.get("result_label", ""))}结果</span>'
            "</div>"
            "</div>"
        )
    st.markdown(
        '<div class="kg-evidence-list">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def _render_path_evidence(items: Sequence[dict]) -> None:
    cards = []
    for item in items:
        cards.append(
            '<div class="kg-path-card">'
            '<div class="kg-path-chain">'
            f'<span class="kg-path-node">{html.escape(str(item.get("root", "")))}'
            f' · {_entity_label(item.get("root_label", ""))}</span>'
            f'<span class="kg-path-relation">{html.escape(str(item.get("first_relationship", "")))} →</span>'
            f'<span class="kg-path-node">{html.escape(str(item.get("intermediate", "")))}'
            f' · {_entity_label(item.get("intermediate_label", ""))}</span>'
            f'<span class="kg-path-relation">{html.escape(str(item.get("second_relationship", "")))} →</span>'
            f'<span class="kg-path-node">{html.escape(str(item.get("target", "")))}'
            f' · {_entity_label(item.get("target_label", ""))}</span>'
            "</div>"
            f'<div class="kg-fact-source">来源 · {_source_text(item.get("source"))}</div>'
            "</div>"
        )
    st.markdown(
        '<div class="kg-evidence-list">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def _render_evidence_tabs(result: dict) -> None:
    specs = _evidence_tab_specs(
        result.get("evidence", []),
        result.get("facts", []),
        result.get("aggregates", []),
        result.get("paths", []),
    )
    if not specs:
        return
    st.markdown(
        '<div class="kg-section-kicker">VERIFIABLE EVIDENCE</div>',
        unsafe_allow_html=True,
    )
    tabs = st.tabs(
        [f"{label} · {count}" for _key, label, count in specs]
    )
    for tab, (key, _label, _count) in zip(tabs, specs, strict=True):
        with tab:
            if key == "relations":
                _render_relation_evidence(result["evidence"])
            elif key == "facts":
                _render_fact_evidence(result["facts"])
            elif key == "aggregates":
                _render_aggregate_evidence(result["aggregates"])
            elif key == "paths":
                _render_path_evidence(result["paths"])


def _render_audit_panel(verification: Optional[dict]) -> None:
    if not verification:
        return
    status = verification.get("status")
    passed = status == "passed"
    class_name = "kg-audit-banner" if passed else "kg-audit-banner fallback"
    title = "回答已通过证据覆盖审核" if passed else "回答已回退为证据化表述"
    message = str(
        verification.get(
            "message",
            "已逐项核对本轮图谱证据。",
        )
    )
    st.markdown(
        '<div class="kg-section-kicker">DETERMINISTIC AUDIT</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        (
            f'<div class="{class_name}">'
            f'<span class="kg-audit-mark">{"✓" if passed else "!"}</span>'
            '<div class="kg-audit-message">'
            f"<strong>{title}</strong>"
            f"<span>{html.escape(message)}</span>"
            "</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )
    checked = int(verification.get("checked_items", 0) or 0)
    matched = int(verification.get("matched_items", 0) or 0)
    st.progress(
        _verification_ratio(verification),
        text=f"证据覆盖 {matched}/{checked}",
    )
    audit_columns = st.columns(4, gap="small")
    audit_metrics = (
        ("关系", int(verification.get("evidence_items", 0) or 0)),
        ("属性", int(verification.get("fact_items", 0) or 0)),
        ("统计", int(verification.get("aggregate_items", 0) or 0)),
        ("路径", int(verification.get("path_items", 0) or 0)),
    )
    for column, (label, value) in zip(
        audit_columns,
        audit_metrics,
        strict=True,
    ):
        with column:
            st.metric(label, str(value))
    missing_sources = int(
        verification.get("missing_sources", 0) or 0
    )
    if missing_sources:
        st.caption(
            f"其中 {missing_sources} 项图谱证据未提供出处，"
            "页面已保留该来源状态。"
        )


def _render_result(result: dict) -> None:
    _render_question_card(result.get("question", ""))
    error = result.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        st.error(message or "问答处理失败，请稍后重试。")
    for warning in result.get("warnings", []):
        st.warning(warning)

    left, right = st.columns([0.82, 1.75], gap="large")
    with left:
        with st.container(border=True):
            _render_panel_heading("处理轨迹", "公开阶段")
            st.markdown(
                _progress_timeline_html(
                    result.get("progress_labels", []),
                    running=False,
                ),
                unsafe_allow_html=True,
            )
            st.caption("只公开阶段、检索策略和决策原因，不展示隐藏推理。")
    with right:
        with st.container(border=True):
            _render_panel_heading("回答", "证据化输出")
            st.markdown(
                _answer_markdown(result.get("answer", "")),
            )
            st.divider()
            _render_metrics(result)

    _render_audit_panel(result.get("verification"))
    _render_evidence_tabs(result)

    session_id = result.get("session_id")
    if session_id:
        with st.expander("会话详情"):
            st.code(session_id, language="text")
            st.caption(
                "会话由 API Key 客户端作用域隔离，最多保留最近 5 轮。"
            )


def main() -> None:
    st.set_page_config(
        page_title="中医知识图谱问答",
        page_icon="🌿",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_styles()

    api_url = os.getenv(
        "GRAPH_QA_STREAMLIT_API_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")
    api_key = os.getenv("GRAPH_QA_STREAMLIT_API_KEY")

    for key, default in (
        ("session_id", None),
        ("progress_labels", []),
        ("last_result", None),
        ("pending_question", None),
        ("flash_message", None),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    st.markdown(HERO_HTML, unsafe_allow_html=True)

    if not api_key:
        st.error("未配置 GRAPH_QA_STREAMLIT_API_KEY。")
        return

    _render_status_bar(st.session_state.session_id, api_url, api_key)
    flash_message = st.session_state.pop("flash_message", None)
    if flash_message:
        st.markdown(
            f'<div class="kg-flash">{html.escape(flash_message)}</div>',
            unsafe_allow_html=True,
        )

    typed_question, submitted = _render_question_form()
    pending_question = st.session_state.pop("pending_question", None)
    question = (
        typed_question
        if submitted and typed_question
        else pending_question
    )

    if question:
        progress_placeholder, answer_placeholder = _render_stream_shell(
            question
        )
        result = _run_query(
            question,
            api_url=api_url,
            api_key=api_key,
            session_id=st.session_state.session_id,
            progress_placeholder=progress_placeholder,
            answer_placeholder=answer_placeholder,
        )
        st.session_state.last_result = result
        st.session_state.progress_labels = result["progress_labels"]
        if result.get("session_id"):
            st.session_state.session_id = result["session_id"]
        st.rerun()

    if st.session_state.last_result:
        _render_result(st.session_state.last_result)
    else:
        _render_empty_state()


if __name__ == "__main__":
    main()
