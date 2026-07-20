"""Streamlit UI:交互式工时分析系统。

核心功能:
  1. 上传 Excel → 批量分析 operation → 下载追加结果列的 Excel
  2. 单条操作描述 → 实时分析(chartcode/决策串/时间/trace)
  3. 可修改结果并确认(飞轮回灌)
  4. Prompt 编辑(侧边栏,热更新)

启动: cd stds_project && .venv/bin/python -m streamlit run stds/ui/app.py
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import streamlit as st

from stds.config.logging_config import setup_logging
setup_logging(level="DEBUG", log_file="stds_debug.log")

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.common_chart import load_common_chart
from stds.domain.models import StdsElement, StdsResult
from stds.llm.pick_value import pick_value
from stds.pipeline.excel_batch import ExcelInputError, ExcelProgress, analyze_excel_bytes
from stds.review.flywheel import on_review_confirmed

# ---------- 初始化(缓存到 session_state) ----------
if "charts" not in st.session_state:
    st.session_state.charts = load_charts()
    st.session_state.cache = AutoCache()
    st.session_state.common_rows = load_common_chart()
    st.session_state.history = []  # 分析历史
if "batch_output" not in st.session_state:
    st.session_state.batch_output = None

PROMPTS_DIR = Path(__file__).parent.parent / "llm" / "prompts"

st.set_page_config(page_title="STDS 工时分析", layout="wide")

# ---------- 侧边栏 ----------
with st.sidebar:
    st.title("⚙️ 设置")

    st.subheader("LLM 配置")
    from stds.config.settings import settings
    st.text(f"Backend: {settings.LLM_BACKEND}")
    st.text(f"Model: {settings.CUSTOM_LLM_MODEL or settings.LLM_MODEL}")
    st.text(f"并发: {settings.CONCURRENCY_LIMIT}")

    st.divider()
    st.subheader("📝 Prompt 编辑(热更新)")
    prompt_files = sorted(PROMPTS_DIR.glob("*.txt"))
    for pf in prompt_files:
        with st.expander(f"📄 {pf.name}"):
            content = pf.read_text(encoding="utf-8")
            new_content = st.text_area(
                "内容", value=content, height=120, key=f"prompt_{pf.name}"
            )
            if st.button(f"💾 保存", key=f"save_{pf.name}"):
                pf.write_text(new_content, encoding="utf-8")
                st.success("已保存,下次分析自动生效")

    st.divider()
    st.subheader("📊 统计")
    st.text(f"缓存命中: {len(st.session_state.cache._store)} 条")
    st.text(f"分析历史: {len(st.session_state.history)} 条")


# ========================================
# 主页:交互式工时分析
# ========================================
st.title("⏱️ STDS 工时分析系统")

# ---------- Excel 批量输入 ----------
st.subheader("📤 Excel 批量分析")
st.caption("工作簿需包含 operation 表头；系统会保留原内容，并追加决策串、逐步的决策选择（trace）、时间三列。")
uploaded_file = st.file_uploader(
    "上传 Excel 文件",
    type=["xlsx"],
    help="支持一个或多个含 operation 字段的工作表",
)

uploaded_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
uploaded_digest = hashlib.sha256(uploaded_bytes).hexdigest() if uploaded_bytes else None
batch_submitted = st.button(
    "🚀 开始批量分析",
    type="primary",
    disabled=uploaded_file is None,
    key="analyze_excel",
)

if batch_submitted and uploaded_file is not None:
    progress_bar = st.progress(0.0)
    progress_text = st.empty()
    progress_details = st.empty()
    timing_rows = []

    def update_batch_progress(progress: ExcelProgress):
        progress_bar.progress(progress.completed_rows / progress.total_rows)
        coverage = (
            f"｜本次覆盖 {progress.affected_rows} 行"
            if progress.affected_rows > 1
            else ""
        )
        progress_text.text(
            f"正在解析：{progress.completed_rows}/{progress.total_rows}"
            f"｜本条 {progress.item_elapsed_s:.2f} 秒{coverage}"
            f"｜累计 {progress.total_elapsed_s:.2f} 秒"
        )
        timing_rows.append(progress.as_preview())
        progress_details.dataframe(
            timing_rows,
            hide_index=True,
            width="stretch",
            height=min(320, 38 + len(timing_rows) * 35),
        )

    try:
        deps = Deps(
            charts=st.session_state.charts,
            cache=st.session_state.cache,
            common_rows=st.session_state.common_rows,
            llm_pick_value=pick_value,
        )
        batch_result = asyncio.run(
            analyze_excel_bytes(
                uploaded_bytes,
                uploaded_file.name,
                deps,
                on_progress=update_batch_progress,
            )
        )
        st.session_state.batch_output = {
            "source_digest": uploaded_digest,
            "filename": batch_result.output_filename,
            "bytes": batch_result.output_bytes,
            "preview": batch_result.preview_rows(),
            "processed": batch_result.processed_count,
            "review": batch_result.review_count,
            "failed": batch_result.failed_count,
            "total_count": batch_result.total_count,
            "total_elapsed_s": batch_result.total_elapsed_s,
            "average_elapsed_s": batch_result.average_elapsed_s,
            "timings": batch_result.timing_rows(),
        }
    except ExcelInputError as exc:
        st.session_state.batch_output = None
        st.error(str(exc))
    except Exception as exc:
        st.session_state.batch_output = None
        st.exception(exc)
    finally:
        progress_bar.empty()
        progress_text.empty()
        progress_details.empty()

batch_output = st.session_state.batch_output
if (
    uploaded_file is not None
    and batch_output is not None
    and batch_output["source_digest"] == uploaded_digest
):
    if batch_output["failed"] or batch_output["review"]:
        st.warning(
            f"已处理 {batch_output['processed']} 条，待复核 {batch_output['review']} 条，"
            f"失败 {batch_output['failed']} 条；原因已写入 trace 列。"
        )
    else:
        st.success(f"已完成 {batch_output['processed']} 条 operation 解析")

    total_col, elapsed_col, average_col = st.columns(3)
    total_col.metric("总计条数", batch_output["total_count"])
    elapsed_col.metric("总耗时", f"{batch_output['total_elapsed_s']:.2f} 秒")
    average_col.metric("平均每条", f"{batch_output['average_elapsed_s']:.2f} 秒")

    with st.expander("⏱️ 逐条处理耗时", expanded=False):
        st.dataframe(
            batch_output["timings"],
            hide_index=True,
            width="stretch",
        )

    st.dataframe(batch_output["preview"], width="stretch")
    st.download_button(
        "⬇️ 下载结果 Excel",
        data=batch_output["bytes"],
        file_name=batch_output["filename"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

st.divider()
st.subheader("✍️ 单条分析（可选）")

# ---------- 单条输入区 ----------
# 使用表单配合单行输入框，让用户在“操作描述”中按 Enter 即可提交。
with st.form("analysis_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        operation = st.text_input(
            "操作描述",
            placeholder="例:拿取一个物体 / 转身90度 / 扫描二维码",
            help="输入完成后按 Enter 发送",
        )
    with col2:
        freq = st.number_input("频率", min_value=0.1, value=1.0, step=0.5)
        line = st.text_input("项目名称", value="")
        station = st.text_input("工位", value="")

    analyze_submitted = st.form_submit_button("🔍 分析", type="primary")

if analyze_submitted and not operation.strip():
    st.warning("请输入操作描述")

# ---------- 分析提交 ----------
if analyze_submitted and operation.strip():
    with st.spinner("分析中..."):
        charts = st.session_state.charts
        cache = st.session_state.cache
        common_rows = st.session_state.common_rows

        async def do_analyze():
            deps = Deps(charts=charts, cache=cache, common_rows=common_rows, llm_pick_value=pick_value)
            el = StdsElement(
                number=1,
                operation_des=operation.strip(),
                line_name=line or "手动输入",
                station_op=station or "手动输入",
                freq=freq,
                norm_key=operation.strip(),
            )
            return await resolve(el, deps)

        result: StdsResult = asyncio.run(do_analyze())

    # ---------- 结果展示 ----------
    st.divider()
    st.subheader("📋 分析结果")

    # 状态标签
    source_colors = {"cache": "🟢", "knn": "🔵", "formula": "🟡", "unresolved": "🔴", "machine": "⚪"}
    source_labels = {"cache": "缓存命中", "knn": "历史匹配", "formula": "公式计算", "unresolved": "待复核", "machine": "设备"}
    color = source_colors.get(result.source.value, "⚪")
    label = source_labels.get(result.source.value, result.source.value)

    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    mcol1.metric("动作代码", result.chartcode or "—")
    mcol2.metric("标准时间", f"{result.time_s:.2f} s")
    mcol3.metric("置信度", f"{result.confidence:.0%}")
    mcol4.metric("来源", f"{color} {label}")

    # 决策详情
    with st.expander("🔍 决策详情", expanded=True):
        dc1, dc2 = st.columns(2)
        with dc1:
            st.text_input("决策串", value=result.decision, disabled=True, key="dec_display")
            st.text_input("增值/非增值", value=result.cv, disabled=True, key="cv_display")
            st.text_input("频率", value=str(result.freq), disabled=True, key="freq_display")
        with dc2:
            st.text_input("操作描述", value=result.element.operation_des, disabled=True, key="op_display")
            st.text_input("需复核", value="是" if result.needs_review else "否", disabled=True, key="review_display")

    # Trace(逐步选择)
    if result.trace:
        with st.expander("📜 逐步选择(Trace)", expanded=False):
            for i, step in enumerate(result.trace):
                var, desc, reason = step
                st.markdown(f"**{var}**: {desc}  \n原因: {reason}")

    # ---------- 复核编辑 ----------
    if result.needs_review or True:  # 始终允许编辑
        with st.expander("✏️ 人工复核(可编辑)", expanded=result.needs_review):
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                edit_cc = st.text_input("动作代码", value=result.chartcode or "", key="edit_cc")
            with ec2:
                edit_dec = st.text_input("决策串", value=result.decision, key="edit_dec")
            with ec3:
                edit_time = st.number_input("时间(s)", value=result.time_s, step=0.1, key="edit_time")

            if st.button("✅ 确认并回灌"):
                # 应用编辑
                from dataclasses import replace
                edited = replace(
                    result,
                    chartcode=edit_cc or result.chartcode,
                    decision=edit_dec,
                    time_s=edit_time,
                    edited=True,
                    needs_review=False,
                    confidence=1.0,
                )
                # 飞轮回灌
                on_review_confirmed(result.element, edited, type("Deps", (), {
                    "cache": st.session_state.cache,
                    "history_index": None,
                    "goldens": [],
                })())
                # 记录历史
                st.session_state.history.append({
                    "操作": operation,
                    "chartcode": edit_cc,
                    "决策": edit_dec,
                    "时间": edit_time,
                    "已编辑": True,
                })
                st.success("✅ 已回灌(缓存已更新,下次相同操作直接命中)")

# ---------- 分析历史 ----------
if st.session_state.history:
    st.divider()
    st.subheader("📜 分析历史")
    st.dataframe(st.session_state.history, width="stretch")
