// wordcloud（npm 名 wordcloud，源 github:timdream/wordcloud2.js，v1.2.3）
// 无官方类型，此处手写本组件实际用到的最小面。
// 该库以 module.exports 导出 WordCloud 函数（带 isSupported/stop 静态成员）。
declare module "wordcloud" {
  export interface WordCloudItem extends Array<string | number> {
    0: string;   // 词文本
    1: number;   // weight（weightFactor=1 时即字号 px）
    2?: number;  // 附加数据（本页透传真实词频）
    [k: number]: string | number;
  }
  export interface WordCloudOptions {
    list: WordCloudItem[];
    gridSize?: number;
    weightFactor?: number | ((pt: WordCloudItem) => number);
    minRotation?: number;
    maxRotation?: number;
    rotationSteps?: number;
    drawOutOfBound?: boolean;
    shrinkToFit?: boolean;
    shuffle?: boolean;
    clearCanvas?: boolean;
    backgroundColor?: string;
    fontFamily?: string;
    fontWeight?: string | number;
    color?: (word: string, weight: number) => string;
    hover?: (item: WordCloudItem | undefined,
             dimension: { x: number; y: number; w: number; h: number } | undefined,
             event: MouseEvent) => void;
    click?: (item: WordCloudItem | undefined,
             dimension: { x: number; y: number; w: number; h: number } | undefined,
             event: MouseEvent) => void;
  }
  const WordCloud: {
    (element: HTMLElement | string, options: WordCloudOptions): boolean | undefined;
    isSupported(): boolean;
    stop(): void;
    minFontSize: number;
  };
  export default WordCloud;
}
