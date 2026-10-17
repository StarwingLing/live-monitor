// 星座连线背景（自托管版，2026-10-10 自 ThreeUI @designcodeio/threeui 1.2.0
// 的 constellation-field 模板裁剪，MIT 许可，来源见工作区 style-reference/threeui 克隆）。
//
// 裁剪原则：只保留可见的背景层（径向渐变底 + 星点连线 canvas + 暗角纱层），
// 剥离全部外部 CDN 依赖——tailwind CDN（生产警告来源）、iconify 图标组件
// （其运行时请求 api.unisvg.com 在境内超时）、gsap/ScrollTrigger（仅服务于
// 被原组件 focus 样式隐藏的演示文案）、Google Fonts。动画核心为纯
// 2D canvas + requestAnimationFrame，原样保留（含 reduced-motion 停用逻辑）。
import { memo } from "react";

const SRC = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
html, body { margin: 0; padding: 0; width: 100%; height: 100%;
             overflow: hidden; background: #070914; }
.bg { position: fixed; inset: 0;
      background: radial-gradient(ellipse at center, #0E1222 0%, #070914 100%); }
.wrap { position: fixed; inset: 0; }
.veil { position: fixed; inset: 0; opacity: .8;
        background: linear-gradient(to bottom, transparent,
                    rgba(14,18,34,.4) 50%, #070914); }
canvas { width: 100%; height: 100%; display: block; }
</style></head>
<body>
<div class="bg"></div>
<div class="wrap"><canvas id="constellationCanvas"></canvas></div>
<div class="veil"></div>
<script>
const canvas = document.getElementById('constellationCanvas');
const ctx = canvas.getContext('2d');
let width, height;
let nodes = [];
const LINK = 160;
const MAX_NODES = window.innerWidth < 768 ? 40 : 85;
let pointer = { x: -1000, y: -1000 };

function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
}
window.addEventListener('resize', () => { resize(); initNodes(); });
resize();

function initNodes() {
    nodes = [];
    for (let i = 0; i < MAX_NODES; i++) {
        nodes.push({
            x: Math.random() * width,
            y: Math.random() * height,
            vx: (Math.random() - 0.5) * 0.3,
            vy: (Math.random() - 0.5) * 0.3,
            radius: Math.random() * 2.4 + 1.8
        });
    }
}
initNodes();

document.addEventListener('mousemove', e => {
    pointer.x = e.clientX; pointer.y = e.clientY;
});
document.addEventListener('mouseleave', () => {
    pointer.x = -1000; pointer.y = -1000;
});

function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

function animateCanvas() {
    ctx.clearRect(0, 0, width, height);
    ctx.lineCap = 'butt';
    ctx.lineJoin = 'miter';
    // 先画连线，星点叠其上更锐利
    ctx.strokeStyle = '#E6C879';
    ctx.lineWidth = 1;
    for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
            const d = dist(nodes[i], nodes[j]);
            if (d < LINK) {
                ctx.globalAlpha = 0.22 + (1 - d / LINK) * 0.55;
                ctx.beginPath();
                ctx.moveTo(nodes[i].x, nodes[i].y);
                ctx.lineTo(nodes[j].x, nodes[j].y);
                ctx.stroke();
            }
        }
    }
    nodes.forEach(node => {
        node.x += node.vx;
        node.y += node.vy;
        if (node.x < 0 || node.x > width) node.vx *= -1;
        if (node.y < 0 || node.y > height) node.vy *= -1;
        // 指针轻引力
        const pd = dist(node, pointer);
        if (pd < 220) {
            node.x -= (node.x - pointer.x) * 0.005;
            node.y -= (node.y - pointer.y) * 0.005;
        }
        // 星点（淡金）：核心 + 光晕
        const pulse = 0.78 + Math.sin(Date.now() * 0.001 + node.x) * 0.22;
        ctx.fillStyle = '#E6C879';
        ctx.globalAlpha = pulse * 0.28;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius * 2.4, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = pulse;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
        ctx.fill();
    });
    ctx.globalAlpha = 1;
    requestAnimationFrame(animateCanvas);
}
const prefersReducedMotion =
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (!prefersReducedMotion) animateCanvas();
<\/script>
</body></html>`;

/** 星座连线背景（iframe srcDoc 自包含，无任何外部网络请求） */
function ConstellationBg({ className }: { className?: string }) {
  return (
    <iframe title="constellation-bg" srcDoc={SRC} sandbox="allow-scripts"
            className={className}
            style={{ display: "block", width: "100%", height: "100%",
                     border: 0, background: "#070914" }} />
  );
}

export default memo(ConstellationBg);
