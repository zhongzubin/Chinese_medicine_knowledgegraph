# -*- coding: utf-8 -*-
"""知识图谱问答 Streamlit 客户端。"""

import json
import os
from typing import Iterator, Optional, Tuple

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


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
    return answer or "正在生成回答…"


def _stage_display_label(event_data: dict) -> Optional[str]:
    stage_label = event_data.get("label")
    plan_label = event_data.get("plan_label")
    if (
        event_data.get("stage") == "plan_cypher"
        and stage_label
        and plan_label
    ):
        return f"{stage_label}（{plan_label}）"
    return stage_label


def main() -> None:
    st.set_page_config(
        page_title="中医知识图谱问答",
        page_icon="🌿",
        layout="wide",
    )
    st.title("中医知识图谱问答")
    st.caption("内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。")

    api_url = os.getenv(
        "GRAPH_QA_STREAMLIT_API_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")
    api_key = os.getenv("GRAPH_QA_STREAMLIT_API_KEY")
    if not api_key:
        st.error("未配置 GRAPH_QA_STREAMLIT_API_KEY。")
        return

    if "session_id" not in st.session_state:
        st.session_state.session_id = None
    if "progress_labels" not in st.session_state:
        st.session_state.progress_labels = []

    if st.button("清空当前会话"):
        session_id = st.session_state.session_id
        if session_id:
            try:
                requests.delete(
                    f"{api_url}/api/v1/sessions/{session_id}",
                    headers={"X-API-Key": api_key},
                    timeout=10,
                )
            except requests.RequestException:
                pass
        st.session_state.session_id = None
        st.session_state.progress_labels = []
        st.rerun()

    question = st.chat_input("请输入中医知识问题，例如：麻黄有什么功效？")
    if not question:
        return

    left, right = st.columns([1, 2], gap="large")
    with left:
        st.subheader("处理进度")
        progress_placeholder = st.empty()
    with right:
        st.subheader("回复")
        answer_placeholder = st.empty()

    label = "正在连接问答服务…"
    answer = ""
    evidence = []
    facts = []
    aggregates = []
    paths = []
    verification = None
    warnings = []
    progress_labels = []
    answer_placeholder.markdown(_answer_markdown(answer))
    progress_placeholder.info(label)

    payload = {"question": question}
    if st.session_state.session_id:
        payload["session_id"] = st.session_state.session_id

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
                    detail = {"error": {"message": "问答服务返回异常状态"}}
                raise RuntimeError(
                    detail.get("error", {}).get("message", "问答服务调用失败")
                )

            for event_name, event_data in parse_sse_lines(
                response.iter_lines(decode_unicode=True)
            ):
                if event_name == "stage":
                    display_label = _stage_display_label(event_data)
                    if display_label and display_label not in progress_labels:
                        progress_labels.append(display_label)
                        progress_placeholder.markdown(
                            "\n".join(
                                f"- {item}" for item in progress_labels
                            )
                        )
                elif event_name == "answer_delta":
                    answer += event_data.get("delta", "")
                    answer_placeholder.markdown(_answer_markdown(answer))
                elif event_name == "evidence":
                    evidence = event_data.get("items", [])
                elif event_name == "facts":
                    facts = event_data.get("items", [])
                elif event_name == "aggregates":
                    aggregates = event_data.get("items", [])
                elif event_name == "paths":
                    paths = event_data.get("items", [])
                elif event_name == "audit":
                    verification = event_data
                elif event_name == "warning":
                    message = event_data.get("message")
                    if message:
                        warnings.append(message)
                elif event_name == "error":
                    message = event_data.get("message", "问答处理失败")
                    answer_placeholder.error(message)
                elif event_name == "done":
                    done_response = event_data.get("response", {})
                    st.session_state.session_id = done_response.get(
                        "session_id",
                        st.session_state.session_id,
                    )
                    final_answer = done_response.get("answer")
                    if final_answer:
                        answer = final_answer
                        answer_placeholder.markdown(final_answer)
                    if done_response.get("verification"):
                        verification = done_response["verification"]
    except (requests.RequestException, RuntimeError) as exc:
        answer_placeholder.error(str(exc))
        return

    st.session_state.progress_labels = progress_labels
    if warnings:
        for warning in warnings:
            st.warning(warning)
    if verification:
        with st.expander("回答校验"):
            if verification.get("status") == "passed":
                st.success(verification.get("message", "回答已通过证据支持校验。"))
            else:
                st.warning(
                    verification.get(
                        "message",
                        "回答未通过证据支持校验，已回退为证据化回答。",
                    )
                )
            st.caption(
                "审核 Agent："
                f"{verification.get('agent', 'evidence_audit')}；"
                f"覆盖 {verification.get('matched_items', 0)}/"
                f"{verification.get('checked_items', 0)} 项证据"
            )
            counts = []
            for key, label in (
                ("evidence_items", "关系"),
                ("fact_items", "属性"),
                ("aggregate_items", "统计"),
                ("path_items", "路径"),
            ):
                count = verification.get(key, 0)
                if count:
                    counts.append(f"{label} {count}")
            if counts:
                st.markdown("；".join(counts))
            missing_sources = verification.get("missing_sources", 0)
            if missing_sources:
                st.caption(f"其中 {missing_sources} 项未提供图谱出处。")
    if evidence:
        with st.expander("图谱证据"):
            for item in evidence:
                source = f"（来源：{item['source']}）" if item.get("source") else ""
                st.markdown(
                    f"- `{item['subject']}:{item['subject_label']}` "
                    f"-[{item['relationship']}]-> "
                    f"`{item['object']}:{item['object_label']}`{source}"
                )
    if facts:
        with st.expander("图谱属性"):
            for item in facts:
                source = (
                    f"（来源：{item['source']}）"
                    if item.get("source")
                    else "（当前图谱未提供出处）"
                )
                st.markdown(
                    f"- `{item['subject']}:{item['subject_label']}` "
                    f"{item['attribute_label']}：{item['value']}{source}"
                )
    if aggregates:
        with st.expander("图谱统计"):
            for item in aggregates:
                st.markdown(
                    f"- {item['description']}共有 {item['count']} 个"
                )
    if paths:
        with st.expander("多跳证据"):
            for item in paths:
                source = f"（来源：{item['source']}）" if item.get("source") else ""
                st.markdown(
                    f"- `{item['root']}:{item['root_label']}` "
                    f"-[{item['first_relationship']}]-> "
                    f"`{item['intermediate']}:{item['intermediate_label']}` "
                    f"-[{item['second_relationship']}]-> "
                    f"`{item['target']}:{item['target_label']}`{source}"
                )
    if st.session_state.session_id:
        st.caption(f"会话：{st.session_state.session_id}")


if __name__ == "__main__":
    main()
