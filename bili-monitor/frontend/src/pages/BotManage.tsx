// 机器人管理页（v2.4.0 拆分自「管理」页的开播账号管理板块，逻辑原样迁移）。
// 页面结构：
//   机器人选择区（v2.5.0 多机器人：按钮=已配置机器人，＋新建，⚙气泡改名；
//      下方全部板块随选中机器人切换）
//   运行状态条（进页自动探测「已保存生效」通道；v2.5.0 起按所选机器人）
//   ① 通道与登录——两种发送方式左右各半的可选卡，点击标题切换使用中的通道，
//      每个板块自带定向「测试连接」
//   ② 群与指令（official 专属：群映射与指令回复；每条指令圆角外框包裹，
//      框内上行=生效群多选下拉+指令输入、下行为自动增高的回复框，
//      移除按钮在框右侧；生效群默认全部群）
//   ②.5 AI 智能回复（official 专属：@未命中指令时 LLM 兜底生成回复，
//      指令命中永远优先；key/地址/模型/人设/频控全可配，默认智谱免费模型）
//   ③ 通知开关与文案——文案标题行自带全局开关（开播/下播），下播行附
//      「下播报告图」开关，防抖在文案下方；「保存配置」下方分隔线之后是
//      主播个人配置卡片（总览页「机器人」开关已开的主播逐卡列出，
//      个人配置=机器人的全部可配置内容，空缺项跟随；主播群号行附
//      「入群核查」开关与「所属机器人」下拉，v2.5.0）
// v2.4.1 起各板块**分区独立保存**：①「保存通道与登录」②「保存指令」
// ③「保存配置」AI「保存 AI 设置」各自只提交本板块字段，互不牵连
// （后端 PATCH 本就逐字段合并）。
// 数据落共享库 qq_notify / qq_bots（live-qq-notify 通知器每 60s 热加载）；
// 房间级个人配置三态（null=跟随全局）。
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { api, fetchMe, patchJson } from "../api";
import { toast } from "../ui";

// 群映射行(v2.4.1 加备注):remark=编辑中文本,remarkSaved=上次落库值
// (失焦/回车时两者不同才提交,避免误触发的重复保存)
interface MapItem {
  group_id: number; group_openid: string; updated_at: string;
  remark: string; remarkSaved: string;
}

// 机器人选择区行(v2.5.0 多机器人)
interface BotItem {
  id: number; name: string; channel: string; official_app_id: string;
}

interface GlobalCfg {
  id: number; name: string;          // v2.5.0 机器人档案标识与显示名
  qq_account: string; onebot_url: string; onebot_token: string;
  webui_url: string; webui_token: string;
  channel: string;                      // 'napcat' | 'official'
  official_app_id: string;
  official_app_secret: string;
  // 群指令自动回复表(空=默认)。v2.4.1 起每条 = {reply, groups},
  // groups 为空数组 = 全部群生效;后端 GET 已归一新格式,?? 仅防御
  command_replies: Record<string, { reply: string; groups: number[] }>;
  notify_on: boolean; notify_off: boolean;
  report_offline: boolean;              // 下播报告图(下播文本后紧随发图)
  template_on: string; template_off: string; debounce_offline: number;
  // AI 智能回复(v2.5.0):@未命中指令时 LLM 兜底;空串=后端内置默认
  // (base=智谱标准 API / model=glm-4.5-flash / 内置人设)
  ai_reply_enabled: boolean;
  ai_api_key: string;
  ai_base_url: string;
  ai_model: string;
  ai_system_prompt: string;
  ai_rate_limit: number;      // 频控:每群每窗口最多次数(默认 3)
  ai_rate_window: number;     // 频控:窗口秒数(默认 300)
}

// 指令自动回复行编辑器的编辑态(v2.4.1 加 groups:生效群号,空=全部群)
interface CmdRow { k: string; v: string; groups: number[] }

// 扫码登录聚合端点的返回(后端代理 NapCat WebUI)
interface QrState {
  phase: "online" | "qr" | "scanned" | "error";
  qrcode: string; message: string;
}

// 运行状态条的数据（/api/qq-notify/status，进页与定向探测共用）
interface Probe {
  online: boolean; account: number | string | null; message: string;
  channel?: string;   // 实际探测的通道(定向探测时回显)
}

// 主播个人配置卡片（GET /api/qq-notify/rooms 的行 + 页面编辑态）
interface RoomCard {
  room_id: number; anchor_name: string; avatar_url: string;
  groups: string;                 // 群号编辑文本(逗号/空格分隔)
  tplOn: string; tplOff: string;
  // 个人覆盖(null=跟随全局)与头像加载失败标记
  // (下播防抖不进个人配置:一律使用全局,2026-10-18 用户定稿)
  ovOn: boolean | null; ovOff: boolean | null; ovReport: boolean | null;
  joinChk: boolean;               // 入群核查开关(v2.5.0)
  botId: number | null;           // 所属机器人(null=默认机器人1)
  avatarErr: boolean; saving: boolean;
}

interface RoomsPayload {
  global: Pick<GlobalCfg, "template_on" | "template_off" | "notify_on"
    | "notify_off" | "report_offline" | "debounce_offline">;
  rooms: {
    room_id: number; anchor_name: string; avatar_url: string;
    group_ids: number[]; template_on: string; template_off: string;
    notify_on: boolean | null; notify_off: boolean | null;
    report_offline: boolean | null;
    join_check: boolean; bot_id: number | null;
  }[];
}

const TPL_VARS_ON = "可用变量(悬浮查看):\n{uname} = 主播名\n{title} = 直播标题\n{url} = 直播间链接\n{cover} = 封面图(写在文案里才会附图;删掉则纯文本)";
const TPL_VARS_OFF = "可用变量(悬浮查看):\n{uname} = 主播名\n{title} = 直播标题(下播时可能为空)\n{url} = 直播间链接\n{duration} = 本次直播时长(写在文案里才会显示;删掉则不带)";
const REPORT_TIP = "开启后:下播文本通知发出后,紧随其后向同批群发送一张本场直播报告图\n(主播信息/场次统计/弹幕排行TOP8/词云,生成约需10~30秒)\n依据监控采集的本场弹幕数据,无采集数据的场次自动跳过";
const AI_TIP = "开启后:群里 @机器人 的消息若未命中指令表,正文会交给大模型生成回复\n(指令命中永远优先;回复自动附带当前开播状态)\n频控次数与窗口可在下方调整;生成失败静默放弃,不打扰群里";
// 常见国内模型服务商预设(2026-10-18 核对各家文档):选定即回填接口地址与
// 建议模型(均可手改);智谱标准=清空两字段走内置默认(免费 glm-4.5-flash)。
// 均为 OpenAI Chat Completions 兼容 + Bearer 鉴权,后端客户端通用。
const AI_PROVIDERS: { label: string; base: string; model: string }[] = [
  { label: "智谱标准 API(bigmodel.cn,免费 glm-4.5-flash)", base: "", model: "" },
  { label: "Z.ai(智谱国际站)", base: "https://api.z.ai/api/paas/v4",
    model: "glm-4.6" },
  { label: "DeepSeek", base: "https://api.deepseek.com", model: "deepseek-chat" },
  { label: "通义千问(阿里百炼)",
    base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { label: "Kimi(月之暗面)", base: "https://api.moonshot.cn/v1",
    model: "kimi-k2.6" },
  { label: "硅基流动 SiliconFlow(聚合多模型)",
    base: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen3-8B" },
];

const JOIN_TIP = "开启后:有人申请加入该主播配置的群时,机器人把申请人的验证回答当B站ID,\n查该ID在本主播直播间的粉丝牌(名称/等级)与最近5条弹幕,发到这些群供管理员参考\n前置:机器人须为该群管理员(群主在群设置里指定),否则收不到入群申请事件\n仅官方通道支持;不做自动审批,只发通知";

export default function BotManage() {
  const [admin, setAdmin] = useState(true);   // 导航已限管理员，进来再核一次
  // 机器人选择区(v2.5.0):列表 + 当前选中 + 齿轮改名气泡(草稿按 id 存)
  const [bots, setBots] = useState<BotItem[]>([]);
  const [curBot, setCurBot] = useState<number | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [nameDraft, setNameDraft] = useState<Record<number, string>>({});
  const [g, setG] = useState<GlobalCfg | null>(null);
  // 三个板块各自独立的保存中状态(v2.4.1 分区保存,互不牵连)
  const [savingChan, setSavingChan] = useState(false);
  const [savingCmds, setSavingCmds] = useState(false);
  const [savingNotify, setSavingNotify] = useState(false);
  // AI 智能回复板块独立保存状态(v2.5.0,同分区保存口径)
  const [savingAi, setSavingAi] = useState(false);
  // 状态条数据(进页自动探测一次「已保存生效」的通道;保存成功后刷新)
  const [probe, setProbe] = useState<Probe | null>(null);
  // 左右通道板块各自的「测试连接」结果(按 ?channel= 定向探测,互不影响)
  const [chMsg, setChMsg] = useState<{ napcat: string; official: string }>(
    { napcat: "", official: "" });
  const [chBusy, setChBusy] = useState<{ napcat: boolean; official: boolean }>(
    { napcat: false, official: false });
  // 扫码登录区:qrOn=轮询中;qrData=二维码图片 dataURL
  const [qrOn, setQrOn] = useState(false);
  const [qrState, setQrState] = useState<QrState | null>(null);
  const [qrData, setQrData] = useState("");
  const qrTimer = useRef<number | null>(null);
  const qrFreshTimer = useRef<number | null>(null);   // 二维码保鲜定时器
  // 群映射获取(official):输入群号 → 绑定会话轮询 → 映射列表
  const [bindGid, setBindGid] = useState("");
  const [binding, setBinding] = useState(false);
  const [bindMsg, setBindMsg] = useState("");
  const [mapItems, setMapItems] = useState<MapItem[]>([]);
  const bindTimer = useRef<number | null>(null);
  // 指令自动回复行编辑器:{k:指令, v:回复, groups:生效群号(空=全部)},
  // 随「保存配置」提交;cmdDrop=群下拉展开中的行下标(null=全部收起)
  const [cmdRows, setCmdRows] = useState<CmdRow[]>([]);
  const [cmdDrop, setCmdDrop] = useState<number | null>(null);
  // 主播个人配置卡片(总览页机器人开关已开的主播)
  const [roomCards, setRoomCards] = useState<RoomCard[] | null>(null);

  useEffect(() => {
    fetchMe().then((m) => setAdmin(m.role === "admin"))
      .catch(() => setAdmin(false));
    loadBots().then((first) => {
      if (first != null) switchBot(first);
    });
    loadRooms();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- 机器人选择区(v2.5.0):载列表/切换/新建/改名 ----

  async function loadBots(): Promise<number | null> {
    try {
      const r = await api<{ items: BotItem[] }>("/api/qq-notify/bots");
      setBots(r.items);
      setCurBot((cur) => (cur != null && r.items.some((b) => b.id === cur)
        ? cur : (r.items[0]?.id ?? null)));
      return r.items[0]?.id ?? null;
    } catch (e: any) {
      toast("机器人列表加载失败: " + e.message);
      return null;
    }
  }

  // 切换选中机器人:重载该机器人的完整配置与指令行编辑器
  async function switchBot(id: number) {
    setCurBot(id);
    stopQr();   // 离开机器人即停上一位的扫码轮询
    try {
      const r = await api<GlobalCfg>(`/api/qq-notify/bot/${id}`);
      setG(r);
      setCmdRows(Object.entries(r.command_replies || {})
        .map(([k, e]) => ({ k, v: e?.reply ?? "", groups: e?.groups ?? [] })));
    } catch (e: any) {
      toast("机器人配置加载失败: " + e.message);
      return;
    }
    // 进页/切机器人自动探测一次(状态条先给答案,不用等管理员点按钮);
    // 结果同时回显到「已保存生效」通道的板块内
    testConn().then((p) => {
      if (p?.channel === "napcat" || p?.channel === "official") {
        setChMsg((m) => ({ ...m, [p.channel!]: (p.online ? "✅ " : "⚠️ ") + p.message }));
      }
    });
  }

  async function addBot() {
    try {
      const r = await api<{ id: number; name: string }>("/api/qq-notify/bots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      toast(`已新建「${r.name}」,各板块默认值,按需配置后保存`);
      await loadBots();
      switchBot(r.id);
    } catch (e: any) {
      toast("新建机器人失败: " + e.message);
    }
  }

  async function renameBot(id: number) {
    const name = (nameDraft[id] ?? "").trim();
    if (!name) {
      toast("机器人名称不能为空");
      return;
    }
    try {
      await patchJson(`/api/qq-notify/bots/${id}`, { name });
      const items = await api<{ items: BotItem[] }>("/api/qq-notify/bots");
      setBots(items.items);
      if (curBot === id) {
        setG((cur) => cur ? { ...cur, name } : cur);   // 当前位同步显示名
      }
      toast("名称已保存");
    } catch (e: any) {
      toast("改名失败: " + e.message);
    }
  }

  // 卸载清理轮询
  useEffect(() => () => {
    if (qrTimer.current) window.clearInterval(qrTimer.current);
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    if (bindTimer.current) window.clearInterval(bindTimer.current);
  }, []);

  // 群下拉外点收起:点到 .nf-cmdg(按钮+面板)以外即收起展开中的那个
  useEffect(() => {
    if (cmdDrop == null) return;
    const h = (ev: MouseEvent) => {
      if (!(ev.target as HTMLElement).closest(".nf-cmdg")) setCmdDrop(null);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [cmdDrop]);

  // 二维码内容串 → 图片(内容变化才重渲)
  useEffect(() => {
    if (!qrState?.qrcode) { setQrData(""); return; }
    QRCode.toDataURL(qrState.qrcode, { width: 220, margin: 1 })
      .then(setQrData).catch(() => setQrData(""));
  }, [qrState?.qrcode]);

  function set<K extends keyof GlobalCfg>(key: K, val: GlobalCfg[K]) {
    setG((cur) => cur ? { ...cur, [key]: val } : cur);
  }

  const official = g?.channel === "official";

  // 切到官方通道(或切换机器人)即载入已绑定映射列表
  useEffect(() => {
    if (official) loadMap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [official, curBot]);

  // ---- 探测:状态条(已保存生效通道,进页/保存后)与左右板块(定向 ?channel=) ----
  async function testConn(): Promise<Probe | null> {
    try {
      const r = await api<Probe>(
        `/api/qq-notify/status${curBot != null ? `?bot_id=${curBot}` : ""}`);
      setProbe(r);
      return r;
    } catch (e: any) {
      const p = { online: false, account: null, message: "探测失败: " + e.message };
      setProbe(p);
      return p;
    }
  }

  async function testChannel(ch: "napcat" | "official") {
    setChBusy((b) => ({ ...b, [ch]: true }));
    try {
      const r = await api<Probe>(
        `/api/qq-notify/status?channel=${ch}${curBot != null ? `&bot_id=${curBot}` : ""}`);
      setChMsg((m) => ({ ...m, [ch]: (r.online ? "✅ " : "⚠️ ") + r.message }));
    } catch (e: any) {
      setChMsg((m) => ({ ...m, [ch]: "⚠️ 探测失败: " + e.message }));
    } finally {
      setChBusy((b) => ({ ...b, [ch]: false }));
    }
  }

  function pickChannel(ch: string) {
    if (!g || g.channel === ch) return;
    set("channel", ch);
    // 离开 NapCat 通道即停扫码轮询(official 无扫码概念)
    if (ch !== "napcat") stopQr();
  }

  async function pollQr(refresh = false) {
    try {
      const r = await api<QrState>(`/api/qq-notify/qrlogin?`
        + `${refresh ? "refresh=1&" : ""}bot_id=${curBot ?? 1}`);
      setQrState(r);
      if (r.phase === "online") {
        stopQr();
        toast("✅ QQ 扫码登录成功");
        testChannel("napcat");   // 登录后 NapCat 板块立即核对连通与账号
      }
    } catch (e: any) {
      setQrState({ phase: "error", qrcode: "", message: e.message });
    }
  }

  // 二维码保鲜:QQ 扫码码有效期约 2 分钟,每 90 秒强制换新码,
  // 防止页面展示的码过期(扫了提示"二维码已过期")
  function scheduleFresh() {
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    qrFreshTimer.current = window.setTimeout(() => {
      if (qrTimer.current) pollQr(true).then(scheduleFresh);
    }, 90_000);
  }

  function startQr() {
    if (qrTimer.current) return;
    setQrOn(true);
    pollQr().then(scheduleFresh);
    qrTimer.current = window.setInterval(() => pollQr(), 2500);
  }

  function stopQr() {
    if (qrTimer.current) window.clearInterval(qrTimer.current);
    qrTimer.current = null;
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    qrFreshTimer.current = null;
    setQrOn(false);
  }

  // ---- 分区保存(v2.4.1 追加:三个板块各自独立保存,互不牵连) ----

  // 指令表行 → dict(空行跳过;不成对/重复返回 null 并 toast 拒绝)。
  // v2.4.1 每条 = {reply, groups},groups 空数组 = 全部群生效
  function buildCmdDict(): Record<string, { reply: string; groups: number[] }> | null {
    const cr: Record<string, { reply: string; groups: number[] }> = {};
    for (const row of cmdRows) {
      const k = row.k.trim(), v = row.v.trim();
      if (!k && !v) continue;
      if (!k || !v) {
        toast("指令自动回复:指令与回复须成对填写(空行可留)");
        return null;
      }
      if (cr[k] !== undefined) {
        toast(`指令重复:${k}`);
        return null;
      }
      cr[k] = { reply: v, groups: row.groups };
    }
    return cr;
  }

  // 通道与登录板块:只提交本板块字段,保存后生效通道即时切换
  async function saveChannel() {
    if (!g) return;
    setSavingChan(true);
    try {
      // 连接类字段(onebot_url/token、webui_url/token)不提交:页面不展示
      // 输入项,PATCH 未传字段不动库中既有值(默认即生产值)
      const saved = await patchJson<GlobalCfg>(`/api/qq-notify/bot/${curBot ?? 1}`, {
        channel: g.channel,
        official_app_id: g.official_app_id,
        official_app_secret: g.official_app_secret,
        qq_account: g.qq_account,
      });
      setG(saved);
      toast("通道与登录已保存(保存后即切换生效通道)");
      testConn();   // 状态条刷新为保存后的生效通道
    } catch (e: any) {
      toast("通道保存失败: " + e.message);
    } finally {
      setSavingChan(false);
    }
  }

  // 群与指令板块:只提交指令表(回复即时生效,与通知配置互不牵连)
  async function saveCommands() {
    if (!g) return;
    const cr = buildCmdDict();
    if (!cr) return;
    setSavingCmds(true);
    try {
      const saved = await patchJson<GlobalCfg>(`/api/qq-notify/bot/${curBot ?? 1}`, {
        command_replies: cr,
      });
      setG(saved);
      setCmdRows(Object.entries(saved.command_replies || {})
        .map(([k, e]) => ({ k, v: e?.reply ?? "", groups: e?.groups ?? [] })));
      toast("指令已保存(指令回复即时生效)");
    } catch (e: any) {
      toast("指令保存失败: " + e.message);
    } finally {
      setSavingCmds(false);
    }
  }

  // 通知开关与文案板块:只提交本板块字段(通知器 60 秒内热生效)
  async function saveNotify() {
    if (!g) return;
    setSavingNotify(true);
    try {
      const saved = await patchJson<GlobalCfg>(`/api/qq-notify/bot/${curBot ?? 1}`, {
        notify_on: g.notify_on,
        notify_off: g.notify_off,
        report_offline: g.report_offline,
        template_on: g.template_on,
        template_off: g.template_off,
        debounce_offline: Number(g.debounce_offline) || 0,
      });
      setG(saved);
      toast("通知配置已保存(通知器 60 秒内热生效)");
    } catch (e: any) {
      toast("通知配置保存失败: " + e.message);
    } finally {
      setSavingNotify(false);
    }
  }

  // AI 智能回复板块(v2.5.0):只提交 AI 七字段,保存后事件中枢即时生效
  async function saveAi() {
    if (!g) return;
    setSavingAi(true);
    try {
      const saved = await patchJson<GlobalCfg>(`/api/qq-notify/bot/${curBot ?? 1}`, {
        ai_reply_enabled: g.ai_reply_enabled,
        ai_api_key: g.ai_api_key,
        ai_base_url: g.ai_base_url,
        ai_model: g.ai_model,
        ai_system_prompt: g.ai_system_prompt,
        ai_rate_limit: Number(g.ai_rate_limit) || 3,
        ai_rate_window: Number(g.ai_rate_window) || 300,
      });
      setG(saved);
      toast("AI 设置已保存(保存后即时生效)");
    } catch (e: any) {
      toast("AI 设置保存失败: " + e.message);
    } finally {
      setSavingAi(false);
    }
  }

  // 服务商预设选定:回填接口地址与建议模型(两字段均可在下方手改;
  // 选「智谱标准」=清空回内置默认;「自定义」不动现有值)
  function pickAiProvider(base: string) {
    if (!g || base === "__custom") return;
    set("ai_base_url", base);
    const p = AI_PROVIDERS.find((x) => x.base === base);
    set("ai_model", p ? p.model : "");
  }

  // ---- 群映射获取(official) ----
  async function loadMap() {
    try {
      const r = await api<{ items: { group_id: number; group_openid: string;
                                     updated_at: string; remark: string }[] }>(
        `/api/qq-notify/group-map?bot_id=${curBot ?? 1}`);
      setMapItems(r.items.map((m) => ({ ...m, remarkSaved: m.remark })));
    } catch { /* 列表加载失败不打扰(绑定结果另有提示) */ }
  }

  // 备注行内保存:失焦/回车触发,与上次落库值(trim 后)不同才提交
  async function saveRemark(m: MapItem) {
    const remark = m.remark.trim();
    if (remark === m.remarkSaved) return;
    try {
      await patchJson(`/api/qq-notify/group-map/${m.group_id}?bot_id=${curBot ?? 1}`,
                      { remark });
      setMapItems((xs) => xs.map((x) => x.group_id === m.group_id
        ? { ...x, remark, remarkSaved: remark } : x));
      toast(`群 ${m.group_id} 备注已保存`);
    } catch (e: any) {
      toast("备注保存失败: " + e.message);
    }
  }

  async function startBind() {
    const gid = +bindGid;
    if (!gid || gid <= 0) {
      toast("请输入目标 QQ 群号");
      return;
    }
    setBinding(true);
    setBindMsg("等待群事件…请在目标群 @机器人 发任意消息,或由群主把机器人拉进群(2 分钟内有效)");
    try {
      await api("/api/qq-notify/bind-group", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group_id: gid, bot_id: curBot ?? 1 }),
      });
      // 2.5s 轮询绑定状态直到终态
      if (bindTimer.current) window.clearInterval(bindTimer.current);
      bindTimer.current = window.setInterval(async () => {
        try {
          const s = await api<{ state: string; openid: string; message: string }>(
            `/api/qq-notify/bind-status?bot_id=${curBot ?? 1}`);
          if (s.state === "waiting") return;
          window.clearInterval(bindTimer.current!);
          bindTimer.current = null;
          setBinding(false);
          if (s.state === "captured") {
            setBindMsg(`✅ 群 ${gid} 绑定成功(${s.openid.slice(0, 10)}…),房间配置填群号即可直达`);
            loadMap();
          } else {
            setBindMsg("❌ " + (s.message || `绑定未完成(${s.state})`));
          }
        } catch { /* 单次状态查询失败继续轮询 */ }
      }, 2500);
    } catch (e: any) {
      setBinding(false);
      setBindMsg("❌ 发起绑定失败: " + e.message);
    }
  }

  async function unbind(gid: number) {
    try {
      await api(`/api/qq-notify/group-map/${gid}?bot_id=${curBot ?? 1}`,
                { method: "DELETE" });
      toast(`群 ${gid} 已解绑(该群号的房间通知将无法发送,直至重新绑定)`);
      loadMap();
    } catch (e: any) {
      toast("解绑失败: " + e.message);
    }
  }

  // ---- 主播个人配置卡片 ----
  async function loadRooms() {
    try {
      const r = await api<RoomsPayload>("/api/qq-notify/rooms");
      setRoomCards(r.rooms.map((x) => ({
        room_id: x.room_id, anchor_name: x.anchor_name, avatar_url: x.avatar_url,
        groups: x.group_ids.join(", "),
        tplOn: x.template_on, tplOff: x.template_off,
        ovOn: x.notify_on, ovOff: x.notify_off, ovReport: x.report_offline,
        joinChk: x.join_check, botId: x.bot_id,
        avatarErr: false, saving: false,
      })));
    } catch (e: any) {
      toast("主播配置加载失败: " + e.message);
      setRoomCards([]);
    }
  }

  function setRoom<RK extends keyof RoomCard>(room_id: number,
                                              key: RK, val: RoomCard[RK]) {
    setRoomCards((cs) => cs
      ? cs.map((c) => c.room_id === room_id ? { ...c, [key]: val } : c)
      : cs);
  }

  // 开关点击:未覆盖时首次点击 = 转为覆盖并取反(生效值起始);已覆盖 = 直接拨
  function flipRoomSwitch(key: "ovOn" | "ovOff" | "ovReport", room: RoomCard) {
    const globalVal = key === "ovOn" ? !!g?.notify_on
      : key === "ovOff" ? !!g?.notify_off : !!g?.report_offline;
    const cur = room[key];
    setRoom(room.room_id, key, cur === null ? !globalVal : !cur);
  }

  // 「跟随全局」芯片:覆盖 ⇄ 跟随(转覆盖时以当前生效值为起点)
  function toggleInherit(key: "ovOn" | "ovOff" | "ovReport", room: RoomCard) {
    const cur = room[key];
    if (cur !== null) {
      setRoom(room.room_id, key, null);
      return;
    }
    const globalVal = key === "ovOn" ? !!g?.notify_on
      : key === "ovOff" ? !!g?.notify_off : !!g?.report_offline;
    setRoom(room.room_id, key, globalVal);
  }

  function resetFollow(room: RoomCard) {
    setRoomCards((cs) => cs ? cs.map((c) => c.room_id === room.room_id ? {
      ...c, ovOn: null, ovOff: null, ovReport: null,
      tplOn: "", tplOff: "",
    } : c) : cs);
    toast(`${room.anchor_name || room.room_id} 已恢复全部跟随全局(待保存)`);
  }

  async function saveRoom(room: RoomCard) {
    // 群号解析:逗号(全/半角)/空白分隔,只接受正整数字段
    const parts = room.groups.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    if (parts.some((s) => !/^\d+$/.test(s))) {
      toast(`${room.anchor_name || room.room_id}:群号只能包含数字(逗号或空格分隔)`);
      return;
    }
    setRoom(room.room_id, "saving", true);
    try {
      await patchJson(`/api/rooms/${room.room_id}/qq-notify`, {
        group_ids: parts.map(Number).filter((n) => n > 0),
        template_on: room.tplOn,
        template_off: room.tplOff,
        notify_on: room.ovOn,           // null=恢复跟随全局
        notify_off: room.ovOff,
        report_offline: room.ovReport,
        join_check: room.joinChk,       // 入群核查开关(v2.5.0)
        bot_id: room.botId,             // 所属机器人(null=默认机器人1)
        // 下播防抖不提交:个人配置一律使用全局(字段缺省=库中不动)
      });
      toast(`${room.anchor_name || room.room_id} 个人配置已保存(60 秒内热生效)`);
      loadRooms();
    } catch (e: any) {
      toast("保存失败: " + e.message);
    } finally {
      setRoom(room.room_id, "saving", false);
    }
  }

  if (!admin) {
    return (
      <div className="card card-pad manage-block">
        <div className="empty" style={{ padding: "28px 0" }}>仅管理员可访问本页</div>
      </div>
    );
  }

  // 主播卡片开关的显示值:覆盖值优先,否则全局生效值(防抖个人不配置)
  const eff = {
    on: !!g?.notify_on, off: !!g?.notify_off, report: !!g?.report_offline,
  };

  return (
    <>
      <div className="page-head">
        <h2>机器人管理</h2>
        <span className="sub">QQ 通知机器人的通道连接与通知行为（改动 60 秒内热生效，无需重启）</span>
      </div>

      {/* 机器人选择区(v2.5.0):左侧已配置机器人按钮流+新建,右侧齿轮改名气泡;
          下方全部板块随选中机器人切换 */}
      <div className="card card-pad manage-block bt-bots">
        <div className="bt-bots-row">
          {bots.map((b) => (
            <button key={b.id}
                    className={"bt-bot-chip" + (curBot === b.id ? " on" : "")}
                    onClick={() => switchBot(b.id)}>
              {b.name || `机器人${b.id}`}
            </button>
          ))}
          <button className="bt-bot-chip add" title="新建机器人配置"
                  onClick={addBot}>＋</button>
        </div>
        <div className="bt-bots-pop-wrap">
          <button className="btn sm ghost" title="重命名当前机器人"
                  onClick={() => {
                    const b = bots.find((x) => x.id === curBot);
                    setNameDraft((d) => ({ ...d, [curBot!]: b?.name ?? "" }));
                    setRenameOpen((v) => !v);
                  }}>⚙ 重命名</button>
          {renameOpen && curBot != null && (
            <div className="bt-pop">
              <input value={nameDraft[curBot] ?? ""}
                     autoFocus maxLength={20}
                     placeholder="机器人名称"
                     onChange={(e) => setNameDraft((d) => ({
                       ...d, [curBot]: e.target.value }))}
                     onKeyDown={(e) => {
                       if (e.key === "Enter") {
                         renameBot(curBot);
                         setRenameOpen(false);
                       }
                       if (e.key === "Escape") setRenameOpen(false);
                     }} />
              <div className="bt-pop-actions">
                <button className="btn sm"
                        onClick={() => { renameBot(curBot); setRenameOpen(false); }}>
                  保存
                </button>
                <button className="btn sm ghost"
                        onClick={() => setRenameOpen(false)}>取消</button>
              </div>
            </div>
          )}
        </div>
      </div>

      {!g ? <div className="card card-pad"><div className="empty" style={{ padding: "28px 0" }}>加载中…</div></div> : (
        <>
          {/* 运行状态条:反映「已保存生效」的通道(进页自动探测,保存后刷新;
              面板里未保存的选择不影响这里);各通道的按需「测试连接」在其
              下方板块内 */}
          {(() => {
            const effOfficial = (probe?.channel ?? g.channel) === "official";
            return (
          <div className="bt-status">
            <span className="bt-label">生效通道</span>
            <span className={"bt-chan " + (effOfficial ? "official" : "napcat")}>
              {effOfficial ? "官方机器人 API" : "NapCat 协议端"}
            </span>
            <span className={"bt-live" + (probe?.online ? "" : " off")}>
              <span className="dot" />
              {probe == null ? "探测中…" : probe.online ? "在线" : "离线"}
            </span>
            {probe?.online && probe.account != null && probe.account !== "" && (
              <span className="bt-who" title={probe.message}>
                {effOfficial ? "AppID " : "登录号 "}{probe.account}
              </span>
            )}
            {probe && !probe.online && probe.message && (
              <span className="bt-why">{probe.message}</span>
            )}
          </div>
            );
          })()}

          {/* ---- 通道与登录:两种发送方式左右各半,点击标题切换使用中的通道,
                  每个板块自带定向「测试连接」 ---- */}
          <div className="card card-pad manage-block">
            <h3 className="block-title">通道与登录</h3>
            <div className="nf-hint-line">
              供 live-qq-notify 通知服务读取(每 60 秒热加载)。点击板块标题切换
              使用中的通道;切换与凭据改动点下方「保存通道与登录」后生效,
              未使用的通道可先行配置并测试。
            </div>
            <div className="bt-chan-grid">
              {/* 左:NapCat 协议端 */}
              <div className={"bt-chan-card napcat" + (official ? "" : " on")}>
                <button type="button" className="bt-chan-head"
                        onClick={() => pickChannel("napcat")}
                        aria-pressed={!official}>
                  <span className="bt-radio" aria-hidden="true" />
                  <span className="bt-chan-name">NapCat 协议端(扫码)</span>
                  {!official && <span className="bt-using">使用中</span>}
                </button>
                <div className="nf-hint-line" style={{ margin: 0 }}>
                  经服务器上的 NapCat 协议端发送(备用通道)。WebUI 连接参数使用
                  服务器本地默认值(127.0.0.1),无需配置;QQ 号为<strong>预留项
                  </strong>:用于校验实际登录账号、防止登错号发错群,可留空。
                </div>
                <label className="nf-field">
                  <span className="nf-label">QQ 号(预留)</span>
                  <input value={g.qq_account}
                         onChange={(e) => set("qq_account", e.target.value)}
                         placeholder="通知小号 QQ 号,可留空" />
                </label>
                <div className="nf-field">
                  <span className="nf-label">QQ 扫码登录</span>
                  {!qrOn ? (
                    <button className="btn" onClick={startQr}>开始扫码登录</button>
                  ) : (
                    <button className="btn ghost" onClick={stopQr}>停止</button>
                  )}
                </div>
                {(qrOn || qrState) && (
                  <div className="nf-qrbox">
                    {qrState?.phase === "online" ? (
                      <div className="nf-qrmsg ok">✅ QQ 已登录(可点下方「测试连接」核对账号)</div>
                    ) : qrState?.phase === "error" ? (
                      <div className="nf-qrmsg err">⚠️ {qrState.message}</div>
                    ) : qrData ? (
                      <>
                        <img className="nf-qrimg" src={qrData} alt="QQ 登录二维码" />
                        <div className="nf-qrmsg">
                          {qrState?.phase === "scanned"
                            ? "已扫码,请在手机上确认登录…"
                            : "请用小号手机 QQ 扫码登录(码每 90 秒自动刷新,请尽快扫)"}
                        </div>
                      </>
                    ) : (
                      <div className="nf-qrmsg">二维码加载中…</div>
                    )}
                  </div>
                )}
                <div className="bt-probe-row">
                  <button className="btn sm" disabled={chBusy.napcat}
                          onClick={() => testChannel("napcat")}>
                    {chBusy.napcat ? "探测中…" : "测试连接"}
                  </button>
                  {chMsg.napcat && <span className="bt-probe-msg">{chMsg.napcat}</span>}
                </div>
              </div>

              {/* 右:官方机器人 API */}
              <div className={"bt-chan-card official" + (official ? " on" : "")}>
                <button type="button" className="bt-chan-head"
                        onClick={() => pickChannel("official")}
                        aria-pressed={official}>
                  <span className="bt-radio" aria-hidden="true" />
                  <span className="bt-chan-name">官方机器人 API(推荐)</span>
                  {official && <span className="bt-using">使用中</span>}
                </button>
                <div className="nf-hint-line" style={{ margin: 0 }}>
                  凭据在 <a className="nf-link" href="https://q.qq.com"
                    target="_blank" rel="noreferrer">QQ 开放平台</a> 管理端
                  「开发设置」查看;额度单群 1000 条/天,无协议端被风控踢线问题。
                </div>
                <label className="nf-field">
                  <span className="nf-label">AppID</span>
                  <input value={g.official_app_id}
                         onChange={(e) => set("official_app_id", e.target.value)}
                         placeholder="机器人 AppID" />
                </label>
                <label className="nf-field">
                  <span className="nf-label">AppSecret</span>
                  <input value={g.official_app_secret} type="password"
                         onChange={(e) => set("official_app_secret", e.target.value)}
                         placeholder="机器人密钥(管理端「开发设置」)" />
                </label>
                <div className="bt-probe-row">
                  <button className="btn sm" disabled={chBusy.official}
                          onClick={() => testChannel("official")}>
                    {chBusy.official ? "探测中…" : "测试连接"}
                  </button>
                  {chMsg.official && <span className="bt-probe-msg">{chMsg.official}</span>}
                </div>
              </div>
            </div>
            <div className="nf-admin-actions">
              <button className="btn primary" disabled={savingChan}
                      onClick={saveChannel}>
                {savingChan ? "保存中…" : "保存通道与登录"}
              </button>
              <span className="nf-hint-line" style={{ margin: 0 }}>
                仅保存本板块(通道与凭据,保存后生效通道即时切换)
              </span>
            </div>
          </div>

          {/* ---- 群与指令（official 专属）:机器人在群里的触达与互动 ---- */}
          {official && (
            <div className="card card-pad manage-block">
              <h3 className="block-title">群与指令</h3>
              <div className="nf-admin-form">
                <div className="nf-probe">
                  <div className="nf-label">群映射获取</div>
                  <div className="nf-hint-line" style={{ margin: 0 }}>
                    官方通道下房间通知<strong>仍填 QQ 群号</strong>,发送时经这里的映射表
                    自动换算 openid。输入群号并点「开始绑定」后,按提示在目标群做一个
                    动作即可自动完成——<strong>这是平台机制要求的唯一步骤</strong>
                    (openid 只能从机器人事件获取):机器人已在群里就<strong> @机器人
                    发任意消息</strong>;还没进群就由<strong>群主把机器人拉进群</strong>。
                    绑定一次永久有效;获取期间请勿在其他群操作机器人。
                  </div>
                  <div className="nf-probe-bar">
                    <input type="number" value={bindGid}
                           onChange={(e) => setBindGid(e.target.value)}
                           placeholder="目标 QQ 群号" />
                    <button className="btn primary" disabled={binding}
                            onClick={startBind}>
                      {binding ? "绑定中…" : "开始绑定"}
                    </button>
                  </div>
                  {bindMsg && (
                    <div className={"nf-qrmsg " +
                      (bindMsg.startsWith("✅") ? "ok" : "err")}>{bindMsg}</div>
                  )}
                  {mapItems.length > 0 && (
                    <div className="nf-probe-results">
                      {mapItems.map((m) => (
                        <div key={m.group_id} className="nf-maprow">
                          <span className="nf-mapgid">群 {m.group_id}</span>
                          <input className="nf-map-remark" value={m.remark}
                                 placeholder="备注(可选)" maxLength={50}
                                 title="群备注,便于区分群用途;回车或点击输入框外保存"
                                 onChange={(e) => setMapItems(mapItems.map((x) =>
                                   x.group_id === m.group_id
                                     ? { ...x, remark: e.target.value } : x))}
                                 onBlur={() => saveRemark(m)}
                                 onKeyDown={(e) => {
                                   if (e.key === "Enter") e.currentTarget.blur();
                                 }} />
                          <span className="nf-mapoid" title={m.group_openid}>
                            {m.group_openid.slice(0, 10)}…({m.updated_at.slice(0, 16)})
                          </span>
                          <button className="btn sm danger"
                                  onClick={() => unbind(m.group_id)}>解绑</button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                <div className="nf-probe">
                  <div className="nf-label">指令自动回复</div>
                  <div className="nf-hint-line" style={{ margin: 0 }}>
                    群内 @机器人 + 指令 → 自动回复框内内容(指令面板的斜杠指令
                    同样生效);每条指令左侧可勾选<strong>只对哪些群生效</strong>
                    (默认全部群),回复内容支持多行。每群每分钟限回 5 次。
                    编辑后点下方「保存指令」即时生效;
                    <strong>清空全部并保存 = 恢复默认</strong>(默认仅 /连接)。
                  </div>
                  {cmdRows.map((row, i) => (
                    <div className="nf-cmdwrap" key={i}>
                      <div className="nf-cmd-card">
                        <div className="nf-cmd-top">
                          <GroupSelect groups={row.groups} items={mapItems}
                                       open={cmdDrop === i}
                                       onOpen={() => setCmdDrop(cmdDrop === i ? null : i)}
                                       onChange={(gs) => setCmdRows(cmdRows.map((r, j) =>
                                         j === i ? { ...r, groups: gs } : r))} />
                          <input className="nf-cmd-k" value={row.k}
                                 placeholder="/指令"
                                 onChange={(e) => setCmdRows(cmdRows.map((r, j) =>
                                   j === i ? { ...r, k: e.target.value } : r))} />
                        </div>
                        <AutoTextarea value={row.v}
                                      placeholder="回复的消息(可多行,高度随内容增加)"
                                      onChange={(e) => setCmdRows(cmdRows.map((r, j) =>
                                        j === i ? { ...r, v: e.target.value } : r))} />
                      </div>
                      <button className="btn sm danger nf-cmd-x"
                              title="移除此条指令"
                              onClick={() => {
                                // 行删除后下标错位,一并收起下拉
                                setCmdDrop(null);
                                setCmdRows(cmdRows.filter((_, j) => j !== i));
                              }}>
                        ×
                      </button>
                    </div>
                  ))}
                  <div>
                    <button className="btn"
                            onClick={() => setCmdRows([...cmdRows,
                              { k: "", v: "", groups: [] }])}>
                      ＋ 添加指令
                    </button>
                  </div>
                  <div className="nf-admin-actions">
                    <button className="btn primary" disabled={savingCmds}
                            onClick={saveCommands}>
                      {savingCmds ? "保存中…" : "保存指令"}
                    </button>
                    <span className="nf-hint-line" style={{ margin: 0 }}>
                      仅保存指令表(指令回复即时生效)
                    </span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ---- AI 智能回复（official 专属）：@未命中指令时 LLM 兜底 ---- */}
          {official && (
            <div className="card card-pad manage-block">
              <h3 className="block-title">AI 智能回复</h3>
              <div className="nf-admin-form">
                <div className="nf-probe">
                  <div className="nf-label">AI 回复</div>
                  <div className="nf-hint-line" style={{ margin: 0 }}>
                    群里 @机器人 的消息<strong>未命中上方指令表</strong>时，正文交给
                    大模型生成回复（指令命中永远优先）。从「服务商」下拉选择常见
                    国内平台（智谱/DeepSeek/通义/Kimi/Z.ai/硅基流动），选定后自动
                    填入接口地址与<strong>建议模型</strong>（均可手改，模型名以各家
                    控制台为准）；Key 在对应平台控制台创建。默认智谱标准 API 的
                    <strong>免费模型</strong> glm-4.5-flash。回复会自动附带当前
                    开播状态。
                  </div>
                  <div className="bt-tpl-head">
                    <span className="bt-tpl-title nf-tip" title={AI_TIP}>
                      启用 AI 回复 ⓘ
                    </span>
                    <div className="bt-tpl-ctl">
                      <button className={"nf-sw" + (g.ai_reply_enabled ? " on" : "")}
                              role="switch" aria-checked={g.ai_reply_enabled}
                              title={AI_TIP}
                              onClick={() => set("ai_reply_enabled", !g.ai_reply_enabled)}>
                        <span className="nf-knob" />
                      </button>
                    </div>
                  </div>
                  <label className="nf-field">
                    <span className="nf-label">服务商(选定自动填地址与建议模型)</span>
                    <select value={
                              AI_PROVIDERS.some((p) => p.base === (g.ai_base_url || "").trim())
                                ? (g.ai_base_url || "").trim() : "__custom"}
                            onChange={(e) => pickAiProvider(e.target.value)}>
                      {AI_PROVIDERS.map((p) => (
                        <option key={p.label} value={p.base}>{p.label}</option>
                      ))}
                      <option value="__custom">自定义(手动填接口地址)</option>
                    </select>
                  </label>
                  <label className="nf-field">
                    <span className="nf-label">API Key</span>
                    <input value={g.ai_api_key} type="password"
                           onChange={(e) => set("ai_api_key", e.target.value)}
                           placeholder="所选平台控制台创建的 API Key" />
                  </label>
                  <div className="nf-row3">
                    <label className="nf-field">
                      <span className="nf-label">接口地址(随服务商自动填,可手改)</span>
                      <input value={g.ai_base_url}
                             onChange={(e) => set("ai_base_url", e.target.value)}
                             placeholder="https://open.bigmodel.cn/api/paas/v4" />
                    </label>
                    <label className="nf-field">
                      <span className="nf-label">模型(随服务商填建议模型,可手改)</span>
                      <input value={g.ai_model}
                             onChange={(e) => set("ai_model", e.target.value)}
                             placeholder="glm-4.5-flash" />
                    </label>
                  </div>
                  <div className="nf-row3">
                    <label className="nf-field">
                      <span className="nf-label">频控次数(每群每窗口)</span>
                      <input type="number" min={1} max={50} value={g.ai_rate_limit}
                             onChange={(e) => set("ai_rate_limit", +e.target.value)} />
                    </label>
                    <label className="nf-field">
                      <span className="nf-label">频控窗口(秒)</span>
                      <input type="number" min={60} max={86400} value={g.ai_rate_window}
                             onChange={(e) => set("ai_rate_window", +e.target.value)} />
                    </label>
                  </div>
                  <label className="nf-field">
                    <span className="nf-label">人设提示词(空=内置默认;回复自动附带开播状态)</span>
                    <AutoTextarea value={g.ai_system_prompt}
                                  placeholder="例:你是直播间群里的通知机器人小助手,回答保持简短友好"
                                  onChange={(e) => set("ai_system_prompt", e.target.value)} />
                  </label>
                  <div className="nf-admin-actions">
                    <button className="btn primary" disabled={savingAi}
                            onClick={saveAi}>
                      {savingAi ? "保存中…" : "保存 AI 设置"}
                    </button>
                    <span className="nf-hint-line" style={{ margin: 0 }}>
                      仅保存本板块(事件中枢即时生效,无需重启)
                    </span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ---- 通知开关与文案:文案标题行自带开关,保存按钮下方挂主播卡片 ---- */}
          <div className="card card-pad manage-block">
            <h3 className="block-title">通知开关与文案</h3>
            <div className="nf-admin-form">
              <div className="bt-tpl-head">
                <span className="bt-tpl-title nf-tip" title={TPL_VARS_ON}>
                  全局开播文案 ⓘ
                </span>
                <div className="bt-tpl-ctl">
                  <span className="bt-tpl-sub">开播通知</span>
                  <button className={"nf-sw" + (g.notify_on ? " on" : "")}
                          role="switch" aria-checked={g.notify_on}
                          title="全局开播通知总开关"
                          onClick={() => set("notify_on", !g.notify_on)}>
                    <span className="nf-knob" />
                  </button>
                </div>
              </div>
              <AutoTextarea value={g.template_on}
                            onChange={(e) => set("template_on", e.target.value)} />

              <div className="bt-tpl-head">
                <span className="bt-tpl-title nf-tip" title={TPL_VARS_OFF}>
                  全局下播文案 ⓘ
                </span>
                <div className="bt-tpl-ctl">
                  <span className="bt-tpl-sub nf-tip" title={REPORT_TIP}>
                    下播报告图 ⓘ
                  </span>
                  <button className={"nf-sw" + (g.report_offline ? " on" : "")}
                          role="switch" aria-checked={g.report_offline}
                          title={REPORT_TIP}
                          onClick={() => set("report_offline", !g.report_offline)}>
                    <span className="nf-knob" />
                  </button>
                </div>
                <div className="bt-tpl-ctl">
                  <span className="bt-tpl-sub">下播通知</span>
                  <button className={"nf-sw" + (g.notify_off ? " on" : "")}
                          role="switch" aria-checked={g.notify_off}
                          title="全局下播通知总开关"
                          onClick={() => set("notify_off", !g.notify_off)}>
                    <span className="nf-knob" />
                  </button>
                </div>
              </div>
              <AutoTextarea value={g.template_off}
                            onChange={(e) => set("template_off", e.target.value)} />

              <label className="nf-field bt-debounce">
                <span className="nf-label">下播防抖(秒)</span>
                <input type="number" min={0} max={86400} value={g.debounce_offline}
                       onChange={(e) => set("debounce_offline", +e.target.value)} />
              </label>

              <div className="nf-admin-actions">
                <button className="btn primary" disabled={savingNotify}
                        onClick={saveNotify}>
                  {savingNotify ? "保存中…" : "保存配置"}
                </button>
                <span className="nf-hint-line" style={{ margin: 0 }}>
                  仅保存本板块(通知开关与文案,通知器 60 秒内热生效)
                </span>
              </div>
            </div>

            <div className="bt-divider" />

            {/* ---- 主播个人配置:总览页「机器人」开关已开的主播逐卡列出 ---- */}
            <h3 className="block-title">主播个人配置</h3>
            <div className="bt-block-sub">
              空缺项跟随上方全局配置;芯片「跟随全局」点亮=该项继承全局
            </div>
            {roomCards == null ? (
              <div className="empty" style={{ padding: "18px 0" }}>加载中…</div>
            ) : roomCards.length === 0 ? (
              <div className="empty" style={{ padding: "18px 0" }}>
                还没有打开机器人开关的主播——在总览页「机器人」列打开开关后,这里
                会出现对应主播的配置卡片
              </div>
            ) : roomCards.map((r) => (
              <div className="bt-room-card" key={r.room_id}>
                <div className="bt-avatar" title={r.anchor_name}>
                  <span>{(r.anchor_name || String(r.room_id)).slice(0, 1)}</span>
                  {r.avatar_url && !r.avatarErr && (
                    <img src={r.avatar_url} alt={`${r.anchor_name} 头像`}
                         onError={() => setRoom(r.room_id, "avatarErr", true)} />
                  )}
                </div>
                <div className="bt-room-main">
                  <div className="bt-room-head">
                    <span className="bt-room-name">{r.anchor_name || r.room_id}</span>
                    <span className="bt-room-id">房间 {r.room_id}</span>
                    <button className="btn sm ghost bt-room-reset"
                            title="清空全部个人覆盖(含文案),恢复跟随全局"
                            onClick={() => resetFollow(r)}>全部跟随全局</button>
                  </div>
                  {/* v2.5.0:主播群号标题字形与开播/下播文案一致(bt-tpl-title),
                      右方挂「入群核查」开关;下方为群号输入与所属机器人下拉 */}
                  <div className="bt-tpl-head">
                    <span className="bt-tpl-title nf-tip" title={JOIN_TIP}>
                      通知群号 ⓘ
                    </span>
                    <div className="bt-tpl-ctl">
                      <span className="bt-tpl-sub nf-tip" title={JOIN_TIP}>
                        入群核查 ⓘ
                      </span>
                      <button className={"nf-sw" + (r.joinChk ? " on" : "")}
                              role="switch" aria-checked={r.joinChk}
                              title={JOIN_TIP}
                              onClick={() => setRoom(r.room_id, "joinChk", !r.joinChk)}>
                        <span className="nf-knob" />
                      </button>
                    </div>
                  </div>
                  <label className="nf-field bt-room-groups">
                    <input value={r.groups}
                           onChange={(e) => setRoom(r.room_id, "groups", e.target.value)}
                           placeholder="如:4242, 8888(逗号或空格分隔)" />
                  </label>
                  <label className="nf-field bt-room-bot">
                    <span className="nf-label">所属机器人</span>
                    <select value={r.botId ?? 1}
                            onChange={(e) => setRoom(r.room_id, "botId", +e.target.value)}>
                      {bots.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name || `机器人${b.id}`}
                        </option>
                      ))}
                    </select>
                  </label>

                  {/* 布局与全局配置一致:开关贴在对应文案标题旁;防抖用全局不在此 */}
                  <div className="bt-tpl-head">
                    <span className="bt-tpl-title nf-tip" title={TPL_VARS_ON}>
                      开播文案 ⓘ
                    </span>
                    <div className="bt-tpl-ctl">
                      <span className="bt-tpl-sub">开播通知</span>
                      <InheritChip on={r.ovOn === null}
                                   onClick={() => toggleInherit("ovOn", r)} />
                      <button className={"nf-sw" + ((r.ovOn ?? eff.on) ? " on" : "")}
                              role="switch" aria-checked={r.ovOn ?? eff.on}
                              onClick={() => flipRoomSwitch("ovOn", r)}>
                        <span className="nf-knob" />
                      </button>
                    </div>
                  </div>
                  <AutoTextarea value={r.tplOn} placeholder={g.template_on}
                                onChange={(e) => setRoom(r.room_id, "tplOn",
                                                         e.target.value)} />

                  <div className="bt-tpl-head">
                    <span className="bt-tpl-title nf-tip" title={TPL_VARS_OFF}>
                      下播文案 ⓘ
                    </span>
                    <div className="bt-tpl-ctl">
                      <span className="bt-tpl-sub nf-tip" title={REPORT_TIP}>
                        下播报告图 ⓘ
                      </span>
                      <InheritChip on={r.ovReport === null}
                                   onClick={() => toggleInherit("ovReport", r)} />
                      <button className={"nf-sw" + ((r.ovReport ?? eff.report) ? " on" : "")}
                              role="switch" aria-checked={r.ovReport ?? eff.report}
                              onClick={() => flipRoomSwitch("ovReport", r)}>
                        <span className="nf-knob" />
                      </button>
                    </div>
                    <div className="bt-tpl-ctl">
                      <span className="bt-tpl-sub">下播通知</span>
                      <InheritChip on={r.ovOff === null}
                                   onClick={() => toggleInherit("ovOff", r)} />
                      <button className={"nf-sw" + ((r.ovOff ?? eff.off) ? " on" : "")}
                              role="switch" aria-checked={r.ovOff ?? eff.off}
                              onClick={() => flipRoomSwitch("ovOff", r)}>
                        <span className="nf-knob" />
                      </button>
                    </div>
                  </div>
                  <AutoTextarea value={r.tplOff} placeholder={g.template_off}
                                onChange={(e) => setRoom(r.room_id, "tplOff",
                                                         e.target.value)} />

                  <div className="bt-room-actions">
                    <button className="btn primary sm" disabled={r.saving}
                            onClick={() => saveRoom(r)}>
                      {r.saving ? "保存中…" : "保存本主播配置"}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

// 「跟随全局」芯片:点亮(默认)=该项继承全局;点击转覆盖/再点回跟随
function InheritChip({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button type="button"
            className={"bt-inherit" + (on ? "" : " ov")}
            title={on ? "当前跟随全局配置,点击转为个人覆盖" : "个人覆盖生效中,点击恢复跟随全局"}
            onClick={onClick}>
      跟随全局
    </button>
  );
}

// 指令生效群多选下拉(v2.4.1):按钮在指令框左方,折叠显示「全部群/已选群」,
// 展开为已绑定群复选列表。不勾任何群 = 全部群生效(默认口径);
// 「全部群」选项=一键清空勾选;限定群时按钮粉边提示当前非默认。
function GroupSelect(props: {
  groups: number[];
  items: { group_id: number; remark?: string }[];   // 传入 mapItems(已绑定群)
  open: boolean;
  onOpen: () => void;
  onChange: (groups: number[]) => void;
}) {
  const { groups, items, open, onOpen, onChange } = props;
  const label = groups.length === 0 ? "全部群"
    : groups.length === 1 ? `群 ${groups[0]}`
    : `群 ${groups[0]} 等${groups.length}群`;
  const toggle = (gid: number) => {
    onChange(groups.includes(gid)
      ? groups.filter((x) => x !== gid) : [...groups, gid]);
  };
  return (
    <div className="nf-cmdg">
      <button type="button"
              className={"nf-cmdg-btn" + (groups.length ? " scoped" : "")}
              title="本条指令只对勾选群发出的指令生效;不勾选=全部群(默认)"
              aria-expanded={open} onClick={onOpen}>
        {label}<span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="nf-cmdg-panel">
          <button type="button"
                  className={"nf-cmdg-opt" + (groups.length ? "" : " on")}
                  onClick={() => onChange([])}>
            <span className="nf-cmdg-box" aria-hidden="true" />全部群
          </button>
          {items.length === 0 ? (
            <div className="nf-cmdg-empty">暂无已绑定群——先在上方完成群绑定</div>
          ) : items.map((m) => (
            <button type="button" key={m.group_id}
                    className={"nf-cmdg-opt" +
                      (groups.includes(m.group_id) ? " on" : "")}
                    onClick={() => toggle(m.group_id)}>
              <span className="nf-cmdg-box" aria-hidden="true" />
              群 {m.group_id}{m.remark ? ` · ${m.remark}` : ""}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// 文案输入框:高度随内容被动扩大(不可手动拖拽、不出滚动条,2026-10-18 定稿)。
// v2.4.1 修复:挂载早期样式/布局未定型时测高会一次性虚高(dev 模式可复现,
// 值不再变就永不纠正)——加 ResizeObserver 在元素宽度变化时兜底重测
function AutoTextarea(props: {
  value: string;
  placeholder?: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // 先归零再按内容回填;宽度变化会改变换行数,由 RO 兜底触发
    const measure = () => {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight + 2}px`;
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [props.value]);
  return (
    <textarea ref={ref} rows={1} value={props.value}
              placeholder={props.placeholder} onChange={props.onChange}
              className="bt-auto-area" />
  );
}
