// v2.3 总览页「通知」列设置弹窗:房间级目标群映射 + 房间专属文案模板。
// 保存走 PATCH /api/rooms/{id}/qq-notify(只提交群/模板,开关仍在列表列);
// 留空的模板在通知器侧回退全局默认(管理页「开播账号管理」维护)。
// v2.3.1 双通道下仍统一填 QQ 群号:official 通道由通知器经群映射表
// (qq_group_map,管理页「群映射获取」绑定)换算 openid 后发送。
import { useEffect, useState } from "react";
import { api, patchJson } from "../api";
import { toast } from "../ui";

interface RoomCfg {
  room_id: number;
  enabled: boolean;
  group_ids: number[];
  template_on: string;
  template_off: string;
  global_template_on: string;
  global_template_off: string;
  channel: string;           // v2.3.1 'napcat' | 'official'(提示文案用)
}

// 模板可用变量(提示文案用;JSX 里需以字面量输出)
const VARS = "{uname} {title} {url}";

export default function NotifyPop({ room, onClose, onSaved }: {
  room: { room_id: number; anchor_name: string; notify_enabled?: boolean };
  onClose: () => void;
  onSaved: () => void;
}) {
  const [cfg, setCfg] = useState<RoomCfg | null>(null);
  const [groups, setGroups] = useState("");
  const [tplOn, setTplOn] = useState("");
  const [tplOff, setTplOff] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<RoomCfg>(`/api/rooms/${room.room_id}/qq-notify`).then((c) => {
      setCfg(c);
      setGroups(c.group_ids.join(", "));
      setTplOn(c.template_on);
      setTplOff(c.template_off);
    }).catch((e) => {
      toast("加载失败: " + e.message);
      onClose();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.room_id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function save() {
    // 群号解析:逗号(全/半角)/空白分隔,只接受正整数字段
    const parts = groups.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    if (parts.some((s) => !/^\d+$/.test(s))) {
      toast("群号只能包含数字(逗号或空格分隔)");
      return;
    }
    const ids = parts.map(Number).filter((n) => n > 0);
    setSaving(true);
    try {
      await patchJson(`/api/rooms/${room.room_id}/qq-notify`, {
        group_ids: ids, template_on: tplOn, template_off: tplOff,
      });
      toast("通知设置已保存(通知器 60 秒内热生效)");
      onSaved();
      onClose();
    } catch (e: any) {
      toast("保存失败:" + e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="nf-mask" onClick={onClose}>
      <div className="nf-pop" onClick={(e) => e.stopPropagation()}>
        <div className="nf-head">
          <div>
            <div className="nf-title">开播通知设置</div>
            <div className="nf-sub">
              {room.anchor_name} · 房间 {room.room_id} ·{" "}
              {room.notify_enabled ? "已开启" : "未开启(开关在总览列表)"}
            </div>
          </div>
          <button className="nf-x" onClick={onClose} aria-label="关闭">×</button>
        </div>
        {!cfg ? (
          <div className="empty" style={{ padding: "28px 0" }}>加载中…</div>
        ) : (
          <div className="nf-body">
            <label className="nf-label">
              通知群号
              <span className="nf-hint">
                (逗号或空格分隔,可多个{cfg.channel === "official"
                  ? ";官方通道下群号须先在管理页「群映射获取」绑定"
                  : ""})
              </span>
            </label>
            <input value={groups} autoFocus
                   onChange={(e) => setGroups(e.target.value)}
                   placeholder="如:4242, 8888" />
            <label className="nf-label">
              开播文案<span className="nf-hint">(留空用全局默认;可用变量:{VARS})</span>
            </label>
            <textarea rows={2} value={tplOn}
                      onChange={(e) => setTplOn(e.target.value)}
                      placeholder={cfg.global_template_on} />
            <label className="nf-label">
              下播文案<span className="nf-hint">(留空用全局默认)</span>
            </label>
            <textarea rows={2} value={tplOff}
                      onChange={(e) => setTplOff(e.target.value)}
                      placeholder={cfg.global_template_off} />
            <div className="nf-actions">
              <button className="btn" onClick={onClose}>取消</button>
              <button className="btn primary" disabled={saving} onClick={save}>
                {saving ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
