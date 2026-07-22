"""Streamlit UI:交互式工时分析系统。

核心功能:
  1. 上传 Excel → 拆解 operation → 逐条工时分析 → 下载结果 Excel
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

from stds.cascade.resolver import Deps
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.common_chart import load_common_chart
from stds.llm.client import llm_runtime
from stds.llm.pick_value import pick_value
from stds.pipeline.excel_batch import (
    NUMBER_HEADER,
    OUTPUT_OPERATION_HEADER,
    STATION_HEADER,
    ExcelInputError,
    ExcelProgress,
    analyze_excel_bytes,
)
from stds.pipeline.operation_analysis import OperationAnalysis, analyze_operation
from stds.review.flywheel import on_review_confirmed

BATCH_OUTPUT_SCHEMA_VERSION = 6
COMMON_CHART_SETTING_VERSION = 1

# ---------- 初始化(缓存到 session_state) ----------
if "charts" not in st.session_state:
    st.session_state.charts = load_charts()
    st.session_state.cache = AutoCache()
    st.session_state.common_rows = load_common_chart()
    st.session_state.history = []  # 分析历史
if "batch_output" not in st.session_state:
    st.session_state.batch_output = None
if st.session_state.get("batch_output_schema_version") != BATCH_OUTPUT_SCHEMA_VERSION:
    st.session_state.batch_output = None
    st.session_state.batch_output_schema_version = BATCH_OUTPUT_SCHEMA_VERSION
if "single_output" not in st.session_state:
    st.session_state.single_output = None
    st.session_state.single_run_id = 0
if st.session_state.get("common_chart_setting_version") != COMMON_CHART_SETTING_VERSION:
    st.session_state.use_common_chart = False
    st.session_state.common_chart_setting_version = COMMON_CHART_SETTING_VERSION

PROMPTS_DIR = Path(__file__).parent.parent / "llm" / "prompts"

st.set_page_config(page_title="STDS 工时分析", layout="wide")

# ---------- 侧边栏 ----------
with st.sidebar:
    st.title("⚙️ 设置")

    st.subheader("LLM 配置")
    from stds.config.settings import settings
    llm_mode_options = ("configured", "ollama")
    llm_mode = st.selectbox(
        "LLM 后端",
        llm_mode_options,
        index=1 if settings.LLM_BACKEND.lower() == "ollama" else 0,
        format_func=lambda value: (
            f"环境配置（{settings.LLM_BACKEND}）"
            if value == "configured"
            else "Ollama"
        ),
        key="llm_mode",
    )
    if llm_mode == "ollama":
        ollama_base_url = st.text_input(
            "Ollama 地址",
            value=settings.OLLAMA_BASE,
            key="ollama_base_url",
        ).strip()
        ollama_model = st.text_input(
            "Ollama 模型",
            value=settings.OLLAMA_LLM_MODEL,
            help="填写本机已通过 ollama pull 安装的模型名，例如 qwen3:14b。",
            key="ollama_model",
        ).strip()
        llm_backend_override = "ollama"
        llm_model_override = ollama_model
    else:
        ollama_base_url = None
        ollama_model = None
        llm_backend_override = None
        llm_model_override = None
        st.caption("当前模型由 .env 中的 LLM_BACKEND 及对应模型配置决定。")
    st.text(f"并发: {settings.CONCURRENCY_LIMIT}")
    use_common_chart = st.toggle(
        "启用 T0.5 Common Chart",
        value=False,
        key="use_common_chart",
        help=(
            "开启时优先使用 common_chart 的高频动作快速匹配；"
            "关闭时跳过 T0.5，继续使用后续 kNN/LLM 工时分析。"
        ),
    )

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

llm_run_signature = (
    llm_mode,
    ollama_base_url or "",
    ollama_model or "",
)


# ========================================
# 主页:交互式工时分析
# ========================================
st.title("⏱️ STDS 工时分析系统")

# ---------- Excel 批量输入 ----------
st.subheader("📤 Excel 批量分析")
st.caption(
    "读取“数据表”的序号/number、工位号/station_op、操作内容/operation 三列；"
    "系统会先拆解人工动作，再逐条计算工时，并输出固定九列结果。"
)
uploaded_file = st.file_uploader(
    "上传 Excel 文件",
    type=["xlsx"],
    help=(
        "读取数据表前三列：序号/number、工位号/station_op、"
        "操作内容/operation；后续已有列会被忽略"
    ),
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
    decomposition_details = st.empty()
    progress_details = st.empty()
    timing_rows = []
    live_decomposition_rows = []

    def update_batch_progress(progress: ExcelProgress):
        progress_bar.progress(progress.overall_ratio)
        progress_text.text(
            f"正在{progress.phase}：{progress.completed_rows}/{progress.total_rows}"
            f"｜本条 {progress.item_elapsed_s:.2f} 秒"
            f"｜累计 {progress.total_elapsed_s:.2f} 秒"
        )
        if progress.phase == "拆解":
            for operation in progress.generated_operations:
                live_decomposition_rows.append(
                    {
                        NUMBER_HEADER: progress.number,
                        STATION_HEADER: progress.station_op,
                        OUTPUT_OPERATION_HEADER: operation,
                    }
                )
            decomposition_details.dataframe(
                live_decomposition_rows,
                hide_index=True,
                width="stretch",
                height=min(360, 38 + len(live_decomposition_rows) * 35),
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
            use_common_chart=use_common_chart,
            llm_pick_value=pick_value,
        )
        with llm_runtime(
            backend=llm_backend_override,
            model=llm_model_override,
            ollama_base_url=ollama_base_url,
        ):
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
            "decomposition": batch_result.decomposition_rows(),
            "details": batch_result.detail_preview_rows(),
            "processed": batch_result.processed_count,
            "review": batch_result.review_count,
            "failed": batch_result.failed_count,
            "total_count": batch_result.total_count,
            "detail_count": batch_result.detail_count,
            "total_elapsed_s": batch_result.total_elapsed_s,
            "decompose_elapsed_s": batch_result.decompose_elapsed_s,
            "analysis_elapsed_s": batch_result.analysis_elapsed_s,
            "average_elapsed_s": batch_result.average_elapsed_s,
            "timings": batch_result.timing_rows(),
            "detail_sheet_name": batch_result.detail_sheet_name,
            "use_common_chart": use_common_chart,
            "llm_run_signature": llm_run_signature,
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
        decomposition_details.empty()
        progress_details.empty()

batch_output = st.session_state.batch_output
if (
    uploaded_file is not None
    and batch_output is not None
    and batch_output["source_digest"] == uploaded_digest
    and batch_output["use_common_chart"] == use_common_chart
    and batch_output["llm_run_signature"] == llm_run_signature
):
    if batch_output["failed"] or batch_output["review"]:
        st.warning(
            f"已处理 {batch_output['processed']} 条，待复核 {batch_output['review']} 条，"
            f"失败 {batch_output['failed']} 条；原因已写入 trace 列。"
        )
    else:
        st.success(
            f"已完成 {batch_output['processed']} 条原始 operation，"
            f"共得到 {batch_output['detail_count']} 条拆解动作"
        )

    total_col, detail_col, elapsed_col, average_col = st.columns(4)
    total_col.metric("原始条数", batch_output["total_count"])
    detail_col.metric("拆解后条数", batch_output["detail_count"])
    elapsed_col.metric("总耗时", f"{batch_output['total_elapsed_s']:.2f} 秒")
    average_col.metric("平均每个原始动作", f"{batch_output['average_elapsed_s']:.2f} 秒")

    phase_col1, phase_col2 = st.columns(2)
    phase_col1.metric("拆解阶段耗时", f"{batch_output['decompose_elapsed_s']:.2f} 秒")
    phase_col2.metric("工时分析阶段耗时", f"{batch_output['analysis_elapsed_s']:.2f} 秒")

    st.download_button(
        "⬇️ 下载完整结果（XLSX）",
        data=batch_output["bytes"],
        file_name=batch_output["filename"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.caption(
        "上方按钮下载完整 XLSX；表格右上角的“Download as CSV”是 Streamlit 自带的当前表格导出。"
    )

    with st.expander("🧩 拆解中间结果", expanded=True):
        st.dataframe(
            batch_output["decomposition"],
            hide_index=True,
            width="stretch",
        )

    with st.expander("📋 拆解动作工时分析明细", expanded=True):
        st.dataframe(
            batch_output["details"],
            hide_index=True,
            width="stretch",
        )

    with st.expander("⏱️ 逐条处理耗时", expanded=False):
        st.dataframe(
            batch_output["timings"],
            hide_index=True,
            width="stretch",
        )

    with st.expander("📊 原始 Excel 行汇总", expanded=False):
        st.dataframe(batch_output["preview"], hide_index=True, width="stretch")
    st.caption(f"下载结果中的逐条拆解与工时明细位于“{batch_output['detail_sheet_name']}”工作表。")
    st.caption(
        "本次分析的 T0.5 Common Chart："
        f"{'已启用' if batch_output['use_common_chart'] else '已关闭'}"
    )
    st.caption(
        "本次 LLM："
        + (
            f"Ollama / {batch_output['llm_run_signature'][2]}"
            if batch_output["llm_run_signature"][0] == "ollama"
            else f"环境配置（{settings.LLM_BACKEND}）"
        )
    )
elif (
    uploaded_file is not None
    and batch_output is not None
    and batch_output["source_digest"] == uploaded_digest
):
    st.info("分析设置已变化，请重新点击“开始批量分析”。")

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
    live_decomposition = st.empty()
    live_progress = st.empty()
    live_details = st.empty()
    live_analysis_rows = {}

    with st.status("正在拆解原始动作…", expanded=True) as single_status:
        charts = st.session_state.charts
        cache = st.session_state.cache
        common_rows = st.session_state.common_rows

        def show_decomposition(split, elapsed_s):
            single_status.update(label="拆解完成，正在逐条进行工时分析…")
            live_decomposition.dataframe(
                [
                    {
                        NUMBER_HEADER: 1,
                        STATION_HEADER: station or "手动输入",
                        OUTPUT_OPERATION_HEADER: child,
                    }
                    for child in split.operations
                ],
                hide_index=True,
                width="stretch",
            )

        def show_item_progress(item, completed, total):
            live_progress.progress(
                completed / total,
                text=f"工时分析：{completed}/{total}｜本条 {item.elapsed_s:.2f} 秒",
            )
            live_analysis_rows[item.index] = {
                "拆解序号": f"{item.index}/{item.total}",
                "operation": item.operation,
                "Chartcode": item.result.chartcode if item.result else "",
                "决策串": item.result.decision if item.result else "",
                "标准时间（秒）": (
                    item.result.time_s
                    if item.result is not None and item.status == "成功"
                    else None
                ),
                "分析耗时（秒）": round(item.elapsed_s, 2),
                "状态": item.status,
            }
            live_details.dataframe(
                [live_analysis_rows[index] for index in sorted(live_analysis_rows)],
                hide_index=True,
                width="stretch",
            )

        async def do_analyze():
            deps = Deps(
                charts=charts,
                cache=cache,
                common_rows=common_rows,
                use_common_chart=use_common_chart,
                llm_pick_value=pick_value,
            )
            with llm_runtime(
                backend=llm_backend_override,
                model=llm_model_override,
                ollama_base_url=ollama_base_url,
            ):
                return await analyze_operation(
                    operation.strip(),
                    deps,
                    line_name=line or "手动输入",
                    station_op=station or "手动输入",
                    freq=freq,
                    on_decomposed=show_decomposition,
                    on_progress=show_item_progress,
                )

        single_analysis: OperationAnalysis = asyncio.run(do_analyze())
        st.session_state.single_run_id += 1
        st.session_state.single_output = {
            "run_id": st.session_state.single_run_id,
            "analysis": single_analysis,
            "use_common_chart": use_common_chart,
            "llm_run_signature": llm_run_signature,
        }
        status_state = "complete" if single_analysis.status == "成功" else "error"
        single_status.update(
            label=(
                f"单条分析完成：{len(single_analysis.items)} 个拆解动作，"
                f"总耗时 {single_analysis.total_elapsed_s:.2f} 秒"
            ),
            state=status_state,
        )

    live_decomposition.empty()
    live_progress.empty()
    live_details.empty()

# ---------- 单条结果展示 ----------
single_output = st.session_state.single_output
if single_output is not None:
    single_analysis: OperationAnalysis = single_output["analysis"]
    single_run_id = single_output["run_id"]
    single_use_common_chart = single_output.get("use_common_chart", False)
    single_llm_signature = single_output.get(
        "llm_run_signature",
        ("configured", "", ""),
    )
    st.divider()
    st.subheader("📋 单条分析结果")

    total_time_display = (
        f"{single_analysis.total_time_s:.2f} s"
        if single_analysis.total_time_s is not None
        else "待复核"
    )
    scol1, scol2, scol3, scol4 = st.columns(4)
    scol1.metric("主体类型", single_analysis.split.actor)
    scol2.metric("拆解动作数", len(single_analysis.items))
    scol3.metric("标准时间总计", total_time_display)
    scol4.metric("处理总耗时", f"{single_analysis.total_elapsed_s:.2f} s")
    st.caption(
        f"拆解阶段 {single_analysis.decompose_elapsed_s:.2f} 秒｜"
        f"工时分析阶段 {single_analysis.analysis_elapsed_s:.2f} 秒｜"
        f"状态：{single_analysis.status}｜T0.5 Common Chart："
        f"{'已启用' if single_use_common_chart else '已关闭'}｜LLM："
        + (
            f"Ollama / {single_llm_signature[2]}"
            if single_llm_signature[0] == "ollama"
            else f"环境配置（{settings.LLM_BACKEND}）"
        )
    )

    if single_analysis.split.error:
        st.warning(
            "拆解阶段出现异常，已回退为原始动作并标记待复核："
            f"{single_analysis.split.error}"
        )

    with st.expander("🧩 拆解中间结果", expanded=True):
        st.dataframe(
            single_analysis.decomposition_rows(),
            hide_index=True,
            width="stretch",
        )

    with st.expander("📊 拆解动作工时明细", expanded=True):
        st.dataframe(
            single_analysis.detail_rows(),
            hide_index=True,
            width="stretch",
        )

    source_colors = {"cache": "🟢", "knn": "🔵", "formula": "🟡", "unresolved": "🔴", "machine": "⚪"}
    source_labels = {"cache": "缓存命中", "knn": "历史匹配", "formula": "公式计算", "unresolved": "待复核", "machine": "设备"}

    for item in single_analysis.items:
        st.markdown(f"#### {item.index}/{item.total}　{item.operation}")
        if item.error or item.result is None:
            st.error(item.error or "该动作分析失败")
            continue

        result = item.result
        color = source_colors.get(result.source.value, "⚪")
        label = source_labels.get(result.source.value, result.source.value)
        mcol1, mcol2, mcol3, mcol4 = st.columns(4)
        mcol1.metric("动作代码", result.chartcode or "—")
        mcol2.metric("标准时间", f"{result.time_s:.2f} s")
        mcol3.metric("置信度", f"{result.confidence:.0%}")
        mcol4.metric("来源", f"{color} {label}")
        st.caption(
            f"决策串：{result.decision or '—'}｜频率：{result.freq}｜"
            f"本条处理耗时：{item.elapsed_s:.2f} 秒｜"
            f"需复核：{'是' if result.needs_review else '否'}"
        )

        if result.trace:
            with st.expander(f"📜 {item.index}/{item.total} 逐步选择（Trace）", expanded=False):
                for step in result.trace:
                    if isinstance(step, (list, tuple)) and len(step) >= 3:
                        var, desc, reason = step[:3]
                    else:
                        var, desc, reason = "trace", str(step), ""
                    st.markdown(f"**{var}**：{desc}  \n原因：{reason}")

        with st.expander(
            f"✏️ {item.index}/{item.total} 人工复核（可编辑）",
            expanded=result.needs_review,
        ):
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                edit_cc = st.text_input(
                    "动作代码",
                    value=result.chartcode or "",
                    key=f"single_{single_run_id}_{item.index}_cc",
                )
            with ec2:
                edit_dec = st.text_input(
                    "决策串",
                    value=result.decision,
                    key=f"single_{single_run_id}_{item.index}_decision",
                )
            with ec3:
                edit_time = st.number_input(
                    "时间(s)",
                    value=result.time_s,
                    step=0.1,
                    key=f"single_{single_run_id}_{item.index}_time",
                )

            if st.button(
                "✅ 确认并回灌",
                key=f"single_{single_run_id}_{item.index}_confirm",
            ):
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
                on_review_confirmed(result.element, edited, type("Deps", (), {
                    "cache": st.session_state.cache,
                    "history_index": None,
                    "goldens": [],
                })())
                item.result = edited
                st.session_state.history.append({
                    "原始操作": single_analysis.original_operation,
                    "操作": item.operation,
                    "chartcode": edit_cc,
                    "决策": edit_dec,
                    "时间": edit_time,
                    "已编辑": True,
                })
                st.success("✅ 当前拆解动作已回灌（下次相同动作直接命中）")
        st.divider()

# ---------- 分析历史 ----------
if st.session_state.history:
    st.divider()
    st.subheader("📜 分析历史")
    st.dataframe(st.session_state.history, width="stretch")
