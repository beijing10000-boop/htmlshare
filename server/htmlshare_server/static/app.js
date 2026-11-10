"use strict";
// HTML 分享站控制台的页面脚本。主题、图标、提示条、二次确认在 ui-kit.js（UIKit，与运维控制台共用）。
// 页面是服务端渲染的：这里只做增强（筛选、复制、确认、上传、画图），不负责取数据。
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // ---- 操作结果提示：服务端跳转回来时带 ?done=xxx，渲染在 #flash 里 ----
  const flash = $("#flash");
  if (flash) {
    UIKit.notify(flash.textContent, { type: flash.dataset.type });
    const url = new URL(location.href);
    if (url.searchParams.has("done")) {
      url.searchParams.delete("done"); // 刷新或收藏时不再重复提示
      history.replaceState(null, "", url);
    }
  }

  // ---- 复制 ----
  // 分享站常常跑在内网 http 上，不是安全上下文，navigator.clipboard 不可用，所以要有 execCommand 兜底
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {}
    }
    const box = document.createElement("textarea");
    box.value = text;
    box.readOnly = true;
    box.className = "offscreen";
    (document.querySelector("dialog:modal") || document.body).append(box);
    box.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {}
    box.remove();
    return ok;
  }

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy], [data-copy-from]");
    if (copy) {
      const source = copy.dataset.copyFrom && document.getElementById(copy.dataset.copyFrom);
      const ok = await copyText(source ? (source.value ?? source.textContent) : copy.dataset.copy);
      UIKit.notify(ok ? "已复制" : "复制失败，请手动选中后复制", { type: ok ? "success" : "error" });
      return;
    }
    const opener = event.target.closest("[data-open-dialog]");
    if (opener) return void document.getElementById(opener.dataset.openDialog)?.showModal();
    const closer = event.target.closest("[data-close-dialog]");
    if (closer) return void closer.closest("dialog")?.close();
    // 用 GET 重新打开当前地址，而不是 reload()：当前页如果是表单提交的结果，reload 会重复提交
    if (event.target.closest("#reload")) location.assign(location.href.split("#")[0]);
  });

  // ---- 表单：危险操作先二次确认；提交后禁用按钮，防止重复提交 ----
  const lock = (form) => {
    for (const button of $$("button[type=submit]", form)) button.disabled = true;
  };
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.id === "upload-form" || form.method === "dialog") return;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      event.preventDefault();
      const ok = await UIKit.confirm({
        title: form.dataset.confirm,
        message: form.dataset.confirmMessage || "",
        confirmText: form.dataset.confirmText || "确认",
        danger: "danger" in form.dataset,
      });
      if (!ok) return;
      form.dataset.confirmed = "1";
      lock(form);
      form.submit();
      return;
    }
    setTimeout(() => lock(form)); // 等表单数据收集完再禁用
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) for (const button of $$("button[type=submit][disabled]")) button.disabled = false;
  });

  // ---- 表格筛选：搜索框 + 胶囊，只在当前页面已有的行里筛 ----
  for (const input of $$("[data-filter-input]")) {
    const table = document.getElementById(input.dataset.filterInput);
    const chips = $(`[data-filter-chips="${table.id}"]`);
    const rows = $$("tbody tr:not([data-empty])", table);
    const empty = $("tr[data-empty]", table);
    let tag = "";
    const apply = () => {
      const query = input.value.trim().toLowerCase();
      let shown = 0;
      for (const row of rows) {
        const hit =
          (!query || row.textContent.toLowerCase().includes(query)) &&
          (!tag || (row.dataset.tags || "").split(" ").includes(tag));
        row.hidden = !hit;
        if (hit) shown++;
      }
      if (empty) empty.hidden = shown > 0;
    };
    input.addEventListener("input", apply);
    chips?.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-chip]");
      if (!chip) return;
      tag = chip.dataset.chip;
      for (const other of $$("[data-chip]", chips)) {
        other.classList.toggle("active", other === chip);
        other.setAttribute("aria-pressed", String(other === chip));
      }
      apply();
    });
    const wanted = new URLSearchParams(location.search).get("show"); // 概览上的「7 天内到期」直接带筛选进来
    if (wanted && chips) $$("[data-chip]", chips).find((chip) => chip.dataset.chip === wanted)?.click();
  }

  // ---- 可见人输入联想：同事、部门、全员 ----
  for (const input of $$("input[data-suggest]")) {
    const list = document.getElementById(input.getAttribute("list"));
    let timer = 0;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const query = input.value.split(/[,，;；、]/).pop().trim();
      if (!query) return void list.replaceChildren();
      timer = setTimeout(async () => {
        try {
          const response = await fetch("/me/suggest?q=" + encodeURIComponent(query));
          if (!response.ok) return;
          const options = ((await response.json()).items || []).map((item) => {
            const option = document.createElement("option");
            option.value = item.value;
            option.label = item.label;
            return option;
          });
          list.replaceChildren(...options);
        } catch {} // 联想失败不影响手工输入
      }, 200);
    });
  }

  // ---- 上传 HTML：失败时对话框不关、已填内容不丢 ----
  const upload = $("#upload-form");
  if (upload) {
    const file = $("#up-file");
    const name = $("#up-name");
    const error = $("#upload-error");
    const submit = $("#upload-submit");
    let named = false; // 用户自己改过页面名，就不再跟着文件名变
    name?.addEventListener("input", () => (named = name.value !== ""));
    file.addEventListener("change", () => {
      if (!name || named || !file.files[0]) return;
      name.value = file.files[0].name
        .replace(/\.html?$/i, "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 64)
        .replace(/-+$/, "");
    });
    upload.addEventListener("submit", async (event) => {
      event.preventDefault();
      const label = submit.textContent;
      error.hidden = true;
      submit.disabled = true;
      submit.textContent = "上传中…";
      try {
        const response = await fetch(upload.action, {
          method: "POST",
          body: new FormData(upload),
          headers: { Accept: "application/json" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.error || `上传失败（HTTP ${response.status}），请稍后重试`);
        if (!data.unresolved?.length) return void location.assign(data.redirect);
        // 发布成功，但有可见人没加上：把原因留在对话框里，再带用户去页面设置
        $("#upload-unresolved").replaceChildren(
          ...data.unresolved.map((item) => {
            const row = document.createElement("li");
            row.textContent = `${item.input}：${item.reason}`;
            return row;
          }),
        );
        $("#upload-fields").hidden = true;
        $("#upload-cancel").hidden = true;
        submit.hidden = true;
        $("#upload-result").hidden = false;
        const go = $("#upload-go");
        go.href = data.redirect;
        go.hidden = false;
        go.focus();
      } catch (problem) {
        error.textContent = problem.message || "上传失败，请检查网络后重试";
        error.hidden = false;
        submit.disabled = false;
        submit.textContent = label;
      }
    });
    if (location.hash === "#upload") upload.closest("dialog").showModal();
  }

  // ---- 访问趋势柱状图 ----
  // 单一序列（每天的打开次数）：柱子最宽 24px、顶部 4px 圆角、从同一条基线长出来；只标最高的那根，
  // 其余靠坐标轴和悬停提示；同一份数据在图下面的表格里也有。
  const SVG_NS = "http://www.w3.org/2000/svg";
  const svgNode = (tag, attributes = {}, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // 纵轴：刻度间隔取 1/2/5×10^n 的整数，最多 5 条线
  function axis(max) {
    const rough = Math.max(max, 1) / 4;
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = Math.max(1, [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough));
    return { step, top: Math.ceil(Math.max(max, 1) / step) * step };
  }

  function showTip(box, x, y, day) {
    hideTip(box);
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    const value = document.createElement("strong");
    value.textContent = `${day.views} 次打开`;
    tip.append(value, document.createElement("br"), `${day.visitors} 位访客 · ${day.date}`);
    box.append(tip);
    const half = tip.offsetWidth / 2; // 靠边的柱子，提示框贴着图的边，不伸出去
    tip.style.left = `${Math.min(Math.max(x, half), box.clientWidth - half)}px`;
    tip.style.top = `${y}px`;
  }
  const hideTip = (box) => $(".chart-tip", box)?.remove();

  function draw(box) {
    const days = JSON.parse(box.dataset.chart);
    const width = box.clientWidth;
    const height = box.clientHeight;
    if (!width || !days.length) return;
    const pad = { left: 30, right: 8, top: 18, bottom: 22 };
    const max = Math.max(...days.map((d) => d.views));
    const { step, top } = axis(max);
    const plotHeight = height - pad.top - pad.bottom;
    const base = pad.top + plotHeight;
    const band = (width - pad.left - pad.right) / days.length;
    const barWidth = Math.max(3, Math.min(24, band * 0.62));
    const y = (value) => base - (value / top) * plotHeight;
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

    for (let value = 0; value <= top; value += step) {
      const line = Math.round(y(value)) + 0.5;
      svg.append(svgNode("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: line, y2: line }));
      svg.append(svgNode("text", { class: "tick", x: pad.left - 8, y: line + 4, "text-anchor": "end" }, value));
    }
    const every = Math.ceil(44 / band); // 横轴日期隔几个标一个，保证不重叠；最后一天（今天）一定标
    const peak = days.findIndex((d) => d.views === max);
    days.forEach((day, index) => {
      const center = pad.left + band * index + band / 2;
      const barTop = y(day.views);
      let bar = null;
      if (day.views > 0) {
        const left = center - barWidth / 2;
        const radius = Math.min(4, barWidth / 2, base - barTop);
        bar = svgNode("path", {
          class: "bar",
          d:
            `M${left},${base}V${barTop + radius}a${radius},${radius} 0 0 1 ${radius},${-radius}` +
            `h${barWidth - 2 * radius}a${radius},${radius} 0 0 1 ${radius},${radius}V${base}Z`,
        });
        svg.append(bar);
        if (index === peak)
          svg.append(svgNode("text", { class: "peak", x: center, y: barTop - 6, "text-anchor": "middle" }, day.views));
      }
      if ((days.length - 1 - index) % every === 0)
        svg.append(svgNode("text", { class: "tick", x: center, y: height - 6, "text-anchor": "middle" }, day.label));
      // 悬停区域是整条竖带，比柱子本身大得多，0 值的日子也能指到
      const hit = svgNode("rect", { class: "hit", x: pad.left + band * index, y: pad.top, width: band, height: plotHeight });
      hit.addEventListener("pointerenter", () => {
        bar?.classList.add("on");
        showTip(box, center, barTop, day);
      });
      hit.addEventListener("pointerleave", () => {
        bar?.classList.remove("on");
        hideTip(box);
      });
      svg.append(hit);
    });
    box.replaceChildren(svg);
  }

  for (const box of $$(".chart[data-chart]")) new ResizeObserver(() => draw(box)).observe(box);
})();
