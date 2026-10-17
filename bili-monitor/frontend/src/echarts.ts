// ECharts 按需注册（3 Mbps 出向对策：只打包含到的图表类型与组件）
import * as echarts from "echarts/core";
import { BarChart, CustomChart, LineChart } from "echarts/charts";
import {
  GridComponent, LegendComponent, TitleComponent, TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart, CustomChart, LineChart, GridComponent, LegendComponent,
  TitleComponent, TooltipComponent, CanvasRenderer,
]);
export default echarts;
export type EChartsOption = echarts.EChartsCoreOption;
