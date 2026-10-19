// ECharts hook（独立于 ui.tsx：仅 Room/Analysis 图表页引入，echarts 代码
// 不进全站首屏 chunk——3 Mbps 出向对策）
import { useEffect, useRef } from "react";
import echarts, { type EChartsOption } from "./echarts";

// 全站图表统一暗色浮窗主题（替换 echarts 默认白底黑字）：
// 框内 = --ink-2 暗色（#0e131d），边框 = --line 半透明，文字 = --bone
export const TOOLTIP_THEME = {
  backgroundColor: "rgba(14, 19, 29, .94)",
  borderColor: "rgba(226, 234, 244, .14)",
  borderWidth: 1,
  padding: [8, 12] as [number, number],
  textStyle: { color: "#e8edf4", fontSize: 12 },
  extraCssText: "border-radius: 8px; box-shadow: 0 8px 24px rgba(2, 6, 14, .5);",
};

export function useChart(option: EChartsOption | null,
                         onInit?: (inst: echarts.ECharts) => void) {
  const ref = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    // v2.5.1：option 归空即销毁实例并清引用。否则两坑：① React 对同位置
    // 同标签 div 只改属性不换节点——骨架屏分支复用原 chart-box 节点时，
    // echarts 命令式插入的 canvas 不属于 React 子树、条件分支清不掉它，
    // 骨架块底下透出旧图；② 残留实例绑在旧节点引用上，数据回来时
    // `inst || init` 短路跳过重建，setOption 落在死实例静默无效（曲线
    // 板块 v2.2 曾在 StrictMode 踩过同款）。其余页面 option 仅首载前为
    // null（加载期间沿用旧图），不受本改动影响。
    if (option == null) {
      inst.current?.dispose();
      inst.current = null;
      return;
    }
    if (!ref.current) return;
    if (!inst.current) {
      inst.current = echarts.init(ref.current);
      onInit?.(inst.current);
    }
    inst.current.setOption(option);
    const onR = () => inst.current?.resize();
    window.addEventListener("resize", onR);
    return () => window.removeEventListener("resize", onR);
  }, [option]);
  useEffect(() => () => {
    inst.current?.dispose();
    // 必须清空引用：dev/StrictMode 挂载期会 setup→cleanup→setup 再来一遍，
    // 若留着一个已 dispose 的实例，下一轮 `inst.current || init` 会短路
    // 跳过重新初始化，setOption 落在死实例上静默无效（曲线板块空白的
    // 根因——本页唯一"带数据挂载"的图表踩中，其余四图挂载时 ref 为空
    // 反而躲过）
    inst.current = null;
  }, []);
  return ref;
}
