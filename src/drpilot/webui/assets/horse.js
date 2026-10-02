/* 点阵马背景动效（位移模糊版）
 *
 * 算法来自 D:/running-pixel-horse 的「位移模糊版」：
 *   位移采样 → 多点模糊 → Bayer 8x8 有序抖动 → 阈值 → 像素点
 * 点阵永远待在网格上，变的只是「这一格里有没有点」，所以风格统一、不会被糊成一片。
 *
 * 用法：
 *   const horse = DrPilotHorse.create(document.getElementById("horse"));
 *   horse.setRunning(true);   // 蓝色 + 奔跑；false = 灰色 + 静止
 */
(function () {
  "use strict";

  const SHEET_URL = "horse-sheet.png";
  const FRAMES = 96;          // 精灵图里的帧数
  const SHEET_COLS = 12;      // 精灵图每行放几帧
  const FRAME_W = 240;        // 单帧尺寸（与 tools/build_horse_sheet.py 一致）
  const FRAME_H = 136;
  const ASPECT = FRAME_W / FRAME_H;

  const PIXEL = 4;            // 一个点阵像素的边长（CSS px）
  const GAP = 1;
  const CELL = PIXEL + GAP;

  // 8x8 Bayer 有序抖动矩阵
  const BAYER = [
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
  ];
  const BAYER_STRENGTH = 0.72;
  const BAYER_THRESHOLD = 0.45;
  const MIN_COVERAGE = 0.02;  // 低于这个值不画，避免马身外面出现抖动噪点

  const FPS = 24;             // 素材节奏
  const MAX_H = 196;          // 马的最大显示高度（CSS px）

  const COLORS = { idle: "150,160,180", run: "59,130,246" };
  const ALPHA = { idle: 0.5, run: 0.62 };

  // 奔跑时的位移模糊：向拖尾一侧（马朝左跑，尾巴在右）多采两格
  const TRAIL = [1, 2];
  const TRAIL_W = [0.26, 0.13];
  const CENTER_W = 1 - TRAIL_W[0] - TRAIL_W[1];

  class HorseBackdrop {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.ready = false;
      this.running = false;
      this.frame = 0;
      this.last = 0;
      this.raf = 0;
      this.w = 0;
      this.h = 0;
      this.gridCols = 0;
      this.gridRows = 0;
      this.cells = 0;
      this.coverage = null;
      this.sheet = null;
      this.reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      this._resizeTimer = 0;
      window.addEventListener("resize", () => {
        clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => this.layout(), 180);
      });
    }

    async load() {
      const image = new Image();
      image.decoding = "async";
      const loaded = new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(new Error("马精灵图加载失败"));
      });
      image.src = SHEET_URL;
      await loaded;
      // 一次性读出整张精灵图的灰度，之后在内存里采样
      const off = document.createElement("canvas");
      off.width = image.naturalWidth;
      off.height = image.naturalHeight;
      const octx = off.getContext("2d", { willReadFrequently: true });
      octx.drawImage(image, 0, 0);
      this.sheet = octx.getImageData(0, 0, off.width, off.height);
      this.ready = true;
      this.layout(true);
    }

    layout(force) {
      if (!this.ready) return;
      const rect = this.canvas.getBoundingClientRect();
      const cssW = Math.max(120, Math.round(rect.width));
      const cssH = Math.max(70, Math.round(rect.height));
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (this.canvas.width !== Math.round(cssW * dpr)) {
        this.canvas.width = Math.round(cssW * dpr);
        this.canvas.height = Math.round(cssH * dpr);
      }
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.w = cssW;
      this.h = cssH;

      const displayH = Math.min(cssH, MAX_H);
      const displayW = Math.min(cssW, displayH * ASPECT);
      const cols = Math.max(8, Math.floor(displayW / CELL));
      const rows = Math.max(8, Math.floor(displayH / CELL));
      const changed = force || cols !== this.gridCols || rows !== this.gridRows;
      this.gridCols = cols;
      this.gridRows = rows;
      this.cells = cols * rows;
      this.originX = Math.max(0, this.w - cols * CELL);
      this.originY = Math.max(0, this.h - rows * CELL);
      if (changed) this.buildCoverage();
      this.render(0, true);
    }

    /* 预计算：每一格、每一帧的覆盖率（双线性采样自精灵图） */
    buildCoverage() {
      const { gridCols: gc, gridRows: gr } = this;
      const data = this.sheet.data;
      const sheetW = this.sheet.width;
      const out = new Uint8Array(FRAMES * gc * gr);
      const scaleX = FRAME_W / gc;
      const scaleY = FRAME_H / gr;
      for (let f = 0; f < FRAMES; f++) {
        const ox = (f % SHEET_COLS) * FRAME_W;
        const oy = Math.floor(f / SHEET_COLS) * FRAME_H;
        const base = f * gc * gr;
        for (let r = 0; r < gr; r++) {
          const fy = (r + 0.5) * scaleY;
          const y0 = Math.min(FRAME_H - 2, Math.max(0, Math.floor(fy)));
          const ty = Math.min(1, Math.max(0, fy - y0));
          for (let c = 0; c < gc; c++) {
            const fx = (c + 0.5) * scaleX;
            const x0 = Math.min(FRAME_W - 2, Math.max(0, Math.floor(fx)));
            const tx = Math.min(1, Math.max(0, fx - x0));
            const i00 = ((oy + y0) * sheetW + ox + x0) * 4;
            const i10 = ((oy + y0) * sheetW + ox + x0 + 1) * 4;
            const i01 = ((oy + y0 + 1) * sheetW + ox + x0) * 4;
            const i11 = ((oy + y0 + 1) * sheetW + ox + x0 + 1) * 4;
            const c0 = data[i00] + (data[i10] - data[i00]) * tx;
            const c1 = data[i01] + (data[i11] - data[i01]) * tx;
            out[base + r * gc + c] = c0 + (c1 - c0) * ty;
          }
        }
      }
      this.coverage = out;
    }

    setRunning(flag) {
      const next = !!flag;
      if (next === this.running) return;
      this.running = next;
      if (next) {
        this.start(this.reduced ? 0 : FPS);
      } else {
        this.frame = 0;      // 停下来时回到起势那一帧，姿态稳定
        this.stop();
        this.render(0, true);
      }
    }

    start(speed) {
      this.speed = speed;
      if (this.raf) return;
      this.last = performance.now();
      const tick = (now) => {
        this.raf = requestAnimationFrame(tick);
        // rAF 的时间戳可能比调度时刻略早（虚拟时间/首帧更明显），
        // dt 必须夹到非负，否则帧号变负 → 采样越界 → 整匹马画不出来
        const dt = Math.max(0, Math.min(0.1, (now - this.last) / 1000));
        this.last = now;
        this.frame = (this.frame + dt * this.speed) % FRAMES;
        if (this.frame < 0) this.frame += FRAMES;
        this.render(this.frame, false);
      };
      this.raf = requestAnimationFrame(tick);
    }

    stop() {
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
    }

    render(frame, force) {
      if (!this.ready || (!force && !this.running)) return;
      const ctx = this.ctx;
      const { gridCols: gc, gridRows: gr, coverage } = this;
      const f0 = ((Math.floor(frame) % FRAMES) + FRAMES) % FRAMES;
      const f1 = (f0 + 1) % FRAMES;
      const frac = frame - Math.floor(frame);
      const base0 = f0 * this.cells;
      const base1 = f1 * this.cells;
      const blur = this.running;
      const rgb = blur ? COLORS.run : COLORS.idle;
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.fillStyle = "rgba(" + rgb + "," + (blur ? ALPHA.run : ALPHA.idle) + ")";
      const ox = this.originX;
      const oy = this.originY;
      for (let r = 0; r < gr; r++) {
        const rowBase = r * gc;
        for (let c = 0; c < gc; c++) {
          const idx = rowBase + c;
          let cov = (coverage[base0 + idx] * (1 - frac) + coverage[base1 + idx] * frac) / 255;
          if (blur) {
            cov *= CENTER_W;
            for (let t = 0; t < TRAIL.length; t++) {
              if (c + TRAIL[t] < gc) {
                const j = rowBase + c + TRAIL[t];
                cov += ((coverage[base0 + j] * (1 - frac) + coverage[base1 + j] * frac) / 255) * TRAIL_W[t];
              }
            }
          }
          if (cov < MIN_COVERAGE) continue;
          const bayer = BAYER[r % 8][c % 8] / 64;
          if (cov + (bayer - 0.5) * BAYER_STRENGTH * 2 > BAYER_THRESHOLD) {
            ctx.fillRect(ox + c * CELL, oy + r * CELL, PIXEL, PIXEL);
          }
        }
      }
    }

    destroy() {
      this.stop();
      window.removeEventListener("resize", this._resizeTimer);
    }
  }

  window.DrPilotHorse = {
    last: null,   // 最近一个实例，方便排查问题
    create: function (canvas) {
      const horse = new HorseBackdrop(canvas);
      window.DrPilotHorse.last = horse;
      horse.load().catch(function (err) {
        // 背景动效出问题绝不能影响主功能：静默隐藏，控制台与 DOM 上留个记录
        canvas.style.display = "none";
        canvas.dataset.horseError = String(err && err.message ? err.message : err);
        if (window.console) console.warn("点阵马背景未启用：" + err);
      });
      return horse;
    },
  };
})();
